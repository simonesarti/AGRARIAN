import numpy as np
import cv2
import onnxruntime as ort
import logging
from time import time


logger = logging.getLogger("main.danger_segmentation")


IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

imagenet_mean_255 = np.array(IMAGENET_MEAN, dtype=np.float32) * 255.0
imagenet_inv_std_255 = 1 / (np.array(IMAGENET_STD, dtype=np.float32) * 255.0)


class _TrtSession:
    """
    Thin wrapper around a TensorRT engine that exposes the same
    .run(output_names, {input_name: data}) interface as ort.InferenceSession,
    so perform_segmentation() works unchanged regardless of backend.

    Imports tensorrt and torch lazily so the module can be imported on
    machines without TRT installed (ONNX path still works).
    """

    def __init__(self, engine_path: str):
        import tensorrt as trt
        import torch

        self._torch = torch

        trt_logger = trt.Logger(trt.Logger.WARNING)
        runtime = trt.Runtime(trt_logger)
        with open(engine_path, "rb") as f:
            engine = runtime.deserialize_cuda_engine(f.read())

        self._engine  = engine
        self._context = engine.create_execution_context()
        self._input_name  = engine.get_tensor_name(0)
        self._output_name = engine.get_tensor_name(1)

        _dtype_map = {
            trt.DataType.FLOAT: torch.float32,
            trt.DataType.HALF:  torch.float16,
            trt.DataType.INT32: torch.int32,
            trt.DataType.INT64: torch.int64,
            trt.DataType.INT8:  torch.int8,
        }
        in_dtype  = _dtype_map.get(engine.get_tensor_dtype(self._input_name),  torch.float32)
        in_shape  = tuple(engine.get_tensor_shape(self._input_name))
        out_dtype = _dtype_map.get(engine.get_tensor_dtype(self._output_name), torch.float32)
        out_shape = tuple(engine.get_tensor_shape(self._output_name))

        # Pre-allocated GPU I/O buffers for TRT inference.
        self._input_gpu  = torch.zeros(in_shape,  dtype=in_dtype,  device="cuda")
        self._output     = torch.zeros(out_shape, dtype=out_dtype, device="cuda")

        H, W = in_shape[2], in_shape[3]

        # Pre-allocated GPU buffer for the raw uint8 frame — receives the H2D transfer
        # (2.76 MB uint8 instead of 11 MB float32, 4× less PCIe bandwidth).
        self._frame_gpu = torch.empty((H, W, 3), dtype=torch.uint8, device="cuda")

        # GPU-side normalization constants, shape (1,3,1,1) for broadcasting over (1,3,H,W).
        self._gpu_mean    = torch.tensor(IMAGENET_MEAN, dtype=torch.float32, device="cuda").view(1, 3, 1, 1) * 255.0
        self._gpu_inv_std = 1.0 / (torch.tensor(IMAGENET_STD, dtype=torch.float32, device="cuda").view(1, 3, 1, 1) * 255.0)

        # Dedicated stream so all preprocessing + TRT kernels are serialised together
        # without blocking the default CUDA stream.
        self._stream = torch.cuda.Stream()

        self._context.set_tensor_address(self._input_name,  self._input_gpu.data_ptr())
        self._context.set_tensor_address(self._output_name, self._output.data_ptr())

        logger.info(
            f"TRT segmentation engine loaded: {engine_path}. "
            f"Input: '{self._input_name}' {in_shape} {in_dtype}, "
            f"Output: '{self._output_name}' {out_shape} {out_dtype}"
        )

    def run_from_frame(self, frame: np.ndarray) -> list:
        """
        Preprocess a raw BGR uint8 frame entirely on the GPU, then run TRT inference.

        CPU work is one H2D transfer of 2.76 MB uint8 (vs 11 MB float32 previously).
        Channel swap (BGR→RGB), type cast, and normalization are all CUDA kernels
        running on self._stream before the TRT kernel is enqueued on the same stream.
        """
        frame_cpu = self._torch.from_numpy(frame)  # zero-copy CPU view of SHM array
        with self._torch.cuda.stream(self._stream):
            # H2D: 2.76 MB uint8. SHM is not pinned so this is synchronous regardless
            # of non_blocking, but 4× less PCIe traffic than transferring float32.
            self._frame_gpu.copy_(frame_cpu)
            # Permute HWC→CHW and reorder channels BGR→RGB in one gather kernel,
            # then copy into the float32 input buffer (contiguous→contiguous, fully coalesced).
            # Two kernels instead of three slice copies.
            self._input_gpu.copy_(
                self._frame_gpu.permute(2, 0, 1)[[2, 1, 0]].unsqueeze(0)
            )
            # In-place normalization: (x - mean*255) * (1/(std*255))
            self._input_gpu.sub_(self._gpu_mean).mul_(self._gpu_inv_std)
        self._context.execute_async_v3(self._stream.cuda_stream)
        self._stream.synchronize()
        return [self._output.cpu().numpy()]

    def run(self, _output_names, input_dict: dict) -> list:
        """Fallback: accept already-preprocessed CHW float32 input (ORT-compatible signature)."""
        (input_data,) = input_dict.values()
        input_tensor = self._torch.as_tensor(input_data, device="cuda")
        self._input_gpu.copy_(input_tensor)
        self._context.execute_async_v3(self._stream.cuda_stream)
        self._stream.synchronize()
        return [self._output.cpu().numpy()]


def create_onnx_segmentation_session(model_ckpt_path: str):
    """
    Load a segmentation model and return (session, input_name, input_shape).

    Accepts either:
      - an ONNX file (.onnx)  → loaded with ONNX Runtime + CUDAExecutionProvider
      - a TensorRT engine (.engine) → loaded via _TrtSession
    """

    if model_ckpt_path.endswith(".engine"):
        session = _TrtSession(model_ckpt_path)
        input_name  = session._input_name
        input_shape = list(session._engine.get_tensor_shape(input_name))
        logger.info(f"TRT segmentation session ready. Input shape: {input_shape}")
        return session, input_name, input_shape

    # ONNX Runtime path
    session_options = ort.SessionOptions()
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session_options.enable_cpu_mem_arena = True
    session_options.enable_mem_pattern = True
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.inter_op_num_threads = 1
    session_options.intra_op_num_threads = 0
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(model_ckpt_path, providers=providers, sess_options=session_options)

    input_name  = session.get_inputs()[0].name
    input_shape = session.get_inputs()[0].shape
    logger.info(f"ONNX Segmentation model input name: {input_name}")
    logger.info(f"ONNX Segmentation model input shape: {input_shape}")

    return session, input_name, input_shape


def preprocess_segmentation_data(frame: np.ndarray):
    """
    Preprocess image data for segmentation model inference.

    Args:
        frame: BGR image (H, W, C) uint8

    Returns:
        np.ndarray: (1, C, H, W) float32, ImageNet-normalised
    """
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    frame_rgb = frame_rgb.astype(np.float32)
    cv2.subtract(frame_rgb, imagenet_mean_255, frame_rgb)
    cv2.multiply(frame_rgb, imagenet_inv_std_255, frame_rgb)
    return np.transpose(frame_rgb, (2, 0, 1))[np.newaxis, ...]


def postprocess_segmentation_output(mask: list, suppress_classes: list[int]):
    """
    Postprocess model output into binary road/vehicle masks.

    Args:
        mask: model output list — first element is (1, H, W) class-label map
        suppress_classes: class indices to remap to background (0)

    Returns:
        roads_mask, vehicles_mask: (H, W) uint8 arrays
    """
    mask = mask[0].squeeze(axis=0)

    if suppress_classes:
        suppress_mask = np.isin(mask, suppress_classes)
        mask[suppress_mask] = 0

    roads_mask    = (mask == 1).astype(np.uint8)
    vehicles_mask = (mask == 2).astype(np.uint8)

    return roads_mask, vehicles_mask


def perform_segmentation(
        session,
        input_name: str,
        frame: np.ndarray,
        segmentation_args: dict,
):
    """
    Run one segmentation inference frame.

    session may be an ort.InferenceSession or a _TrtSession — both expose .run().
    """
    t0 = time()
    if hasattr(session, "run_from_frame"):
        # GPU preprocessing path: H2D + channel swap + normalize + TRT kernel all in one call.
        mask = session.run_from_frame(frame)
        t_gpu = time()
        result = postprocess_segmentation_output(
            mask=mask,
            suppress_classes=segmentation_args["suppress_classes"],
        )
        t_post = time()
        logger.debug(
            f"seg breakdown: gpu_preprocess+infer={1000*(t_gpu-t0):.1f}ms  postprocess={1000*(t_post-t_gpu):.1f}ms"
        )
    else:
        preprocessed_frame = preprocess_segmentation_data(frame)
        t1 = time()
        mask = session.run(None, {input_name: preprocessed_frame})
        t2 = time()
        result = postprocess_segmentation_output(
            mask=mask,
            suppress_classes=segmentation_args["suppress_classes"],
        )
        t3 = time()
        logger.debug(
            f"seg breakdown: preprocess={1000*(t1-t0):.1f}ms  run={1000*(t2-t1):.1f}ms  postprocess={1000*(t3-t2):.1f}ms"
        )
    return result
