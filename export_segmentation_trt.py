#!/usr/bin/env python3
"""
Build a TensorRT FP16 engine from the ONNX segmentation checkpoint and save it
to the engine/ directory so the pipeline can load it at runtime.

The engine is tied to the GPU architecture and TensorRT version it was built on.
Re-run this script any time you change GPU, Docker image, or TensorRT version.

Usage (run inside the project Docker container):

  docker run --rm --gpus all \\
    --entrypoint python \\
    -v ./checkpoints:/app/checkpoints \\
    -v ./engine:/app/engine \\
    agrarian:py312 export_segmentation_trt.py

Optional arguments:
  --onnx PATH         ONNX checkpoint path  (default: checkpoints/segmentation_1280_720.onnx)
  --engine PATH       Output engine path    (default: engine/<stem>.engine)
  --no-fp16           Build in FP32 instead of FP16
  --workspace-gb N    TRT workspace size in GB (default: 2)
"""

import argparse
from pathlib import Path

import tensorrt as trt


def build(onnx_path: str, engine_path: str, fp16: bool = True, workspace_gb: int = 2) -> None:
    logger = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(logger)
    network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    parser = trt.OnnxParser(network, logger)

    with open(onnx_path, "rb") as f:
        if not parser.parse(f.read()):
            for i in range(parser.num_errors):
                print(parser.get_error(i))
            raise RuntimeError(f"Failed to parse ONNX model: {onnx_path}")

    print(f"Parsed ONNX model: {network.num_layers} layers")
    print(f"  Input  : {network.get_input(0).name}  {network.get_input(0).shape}")
    print(f"  Output : {network.get_output(0).name}  {network.get_output(0).shape}")

    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, workspace_gb << 30)

    if fp16 and builder.platform_has_fast_fp16:
        config.set_flag(trt.BuilderFlag.FP16)
        print("FP16 mode enabled")
    else:
        print("FP16 not available on this GPU or disabled — building in FP32")

    print("Building TensorRT engine (this may take a few minutes) ...")
    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError("TensorRT engine build failed — check the logs above")

    Path(engine_path).parent.mkdir(parents=True, exist_ok=True)
    with open(engine_path, "wb") as f:
        f.write(serialized)

    size_mb = Path(engine_path).stat().st_size / 1e6
    print(f"Engine saved to {engine_path}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export segmentation ONNX → TensorRT engine")
    parser.add_argument("--onnx", default="checkpoints/segmentation_1280_720.onnx")
    parser.add_argument("--engine", default=None, help="Output path. Defaults to engine/<stem>.engine")
    parser.add_argument("--no-fp16", action="store_true")
    parser.add_argument("--workspace-gb", type=int, default=2)
    args = parser.parse_args()

    engine_out = args.engine or str(Path("engine") / (Path(args.onnx).stem + ".engine"))
    build(args.onnx, engine_out, fp16=not args.no_fp16, workspace_gb=args.workspace_gb)
