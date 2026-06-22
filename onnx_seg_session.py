"""
Minimal ONNX-Runtime segmentation session context manager (CUDA provider).

Usage as a module:
    from onnx_seg_session import OnnxSegmentationSession

Standalone smoke-test:
    python onnx_seg_session.py [model_path]
"""

import sys

import numpy as np
import onnxruntime as ort


class OnnxSegmentationSession:
    """Context manager that owns an ORT InferenceSession with CUDAExecutionProvider."""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self._session: ort.InferenceSession | None = None
        self.input_name: str = ""
        self.input_shape: list = []

    def __enter__(self) -> "OnnxSegmentationSession":
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.enable_cpu_mem_arena = True
        opts.enable_mem_pattern = True
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 0

        self._session = ort.InferenceSession(
            self.model_path,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            sess_options=opts,
        )
        self.input_name = self._session.get_inputs()[0].name
        self.input_shape = list(self._session.get_inputs()[0].shape)
        return self

    def __exit__(self, *_):
        self._session = None

    def run(self, preprocessed: np.ndarray) -> list:
        """Run inference. preprocessed must be (1, C, H, W) float32."""
        assert self._session is not None, "Session not open — use inside a 'with' block."
        return self._session.run(None, {self.input_name: preprocessed})


if __name__ == "__main__":
    model_path = sys.argv[1] if len(sys.argv) > 1 else "checkpoints/segmentation_1280_720.onnx"

    with OnnxSegmentationSession(model_path) as sess:
        print(f"Loaded  : {model_path}")
        print(f"Input   : '{sess.input_name}' {sess.input_shape}")

        shape = [d if isinstance(d, int) else 1 for d in sess.input_shape]
        dummy = np.zeros(shape, dtype=np.float32)
        out = sess.run(dummy)
        print(f"Output  : {out[0].shape}")
        print("Session OK.")
