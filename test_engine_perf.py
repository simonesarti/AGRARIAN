"""
Measures per-call latency of model.track() with a TRT .engine vs .pt checkpoint.

Run inside the container:
  docker run --rm --gpus all \
    --entrypoint python3 \
    -v $(pwd)/checkpoints:/app/checkpoints \
    -v $(pwd)/engine:/app/engine \
    -v $(pwd)/configs:/app/configs \
    <image> test_engine_perf.py [--checkpoint path] [--n 50] [--warmup 5]
"""
import argparse
import time
import numpy as np
from ultralytics import YOLO

IMGSZ = (736, 1280)   # (H, W) — must match what the engine was compiled for
TRACK_KWARGS = dict(
    imgsz=list(IMGSZ),
    conf=0.35,
    iou=0.5,
    half=True,
    device=0,
    batch=1,
    rect=True,
    tracker="configs/health_monitoring/botsort.yaml",
    verbose=False,
)


def bench(checkpoint: str, n_warmup: int, n_bench: int) -> None:
    print(f"\nLoading: {checkpoint}")
    model = YOLO(checkpoint, task="detect")

    dummy = np.zeros((*IMGSZ, 3), dtype=np.uint8)

    print(f"Warmup ({n_warmup} calls) ...")
    warmup_times = []
    for i in range(n_warmup):
        t0 = time.perf_counter()
        model.track(source=dummy, stream=False, persist=True, **TRACK_KWARGS)
        dt = (time.perf_counter() - t0) * 1000
        warmup_times.append(dt)
        print(f"  warmup {i+1}: {dt:.1f} ms")

    print(f"\nBenchmark ({n_bench} calls) ...")
    times = []
    for i in range(n_bench):
        t0 = time.perf_counter()
        model.track(source=dummy, stream=False, persist=True, **TRACK_KWARGS)
        dt = (time.perf_counter() - t0) * 1000
        times.append(dt)

    times_arr = np.array(times)
    print(f"\nResults for {checkpoint}:")
    print(f"  mean:   {times_arr.mean():.1f} ms")
    print(f"  median: {np.median(times_arr):.1f} ms")
    print(f"  min:    {times_arr.min():.1f} ms")
    print(f"  max:    {times_arr.max():.1f} ms")
    print(f"  p95:    {np.percentile(times_arr, 95):.1f} ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", default="engine/detection_1280_720_yolo11m.engine")
    parser.add_argument("--n", type=int, default=50, help="number of benchmark calls")
    parser.add_argument("--warmup", type=int, default=5, help="number of warmup calls")
    args = parser.parse_args()

    bench(args.checkpoint, args.warmup, args.n)
