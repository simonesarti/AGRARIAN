"""
Annotate MP4 videos with road/vehicle segmentation masks.

Usage:
    python annotate_videos.py --output-dir results/ video1.mp4 video2.mp4 ...

Optional:
    --model            Path to ONNX checkpoint (default: checkpoints/segmentation_1280_720.onnx)
    --alpha            Overlay opacity 0-1 (default: 0.45)
    --suppress-classes Space-separated class indices to zero out before decoding
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

from onnx_seg_session import OnnxSegmentationSession


# Preprocessing constants matching the pipeline (0.5 mean / 0.5 std)
_MEAN_255 = np.array([0.5, 0.5, 0.5], dtype=np.float32) * 255.0
_INV_STD_255 = 1.0 / (np.array([0.5, 0.5, 0.5], dtype=np.float32) * 255.0)

# BGR overlay colours
_ROAD_BGR = np.array([30, 200, 30], dtype=np.float32)     # green
_VEHICLE_BGR = np.array([30, 30, 220], dtype=np.float32)  # red


def _preprocess(frame_bgr: np.ndarray, target_hw: tuple[int, int]) -> np.ndarray:
    """Resize + BGR→RGB + normalise → (1, 3, H, W) float32."""
    h, w = target_hw
    resized = cv2.resize(frame_bgr, (w, h), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
    cv2.subtract(rgb, _MEAN_255, rgb)
    cv2.multiply(rgb, _INV_STD_255, rgb)
    return np.transpose(rgb, (2, 0, 1))[np.newaxis, ...]


def _postprocess(
    raw_output: list,
    suppress_classes: list[int] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Decode model output into (roads_mask, vehicles_mask), both uint8 (H, W)."""
    mask = raw_output[0].squeeze(0)
    if suppress_classes:
        mask[np.isin(mask, suppress_classes)] = 0
    return (mask == 1).astype(np.uint8), (mask == 2).astype(np.uint8)


def _annotate(
    frame_bgr: np.ndarray,
    roads_mask: np.ndarray,
    vehicles_mask: np.ndarray,
    alpha: float,
) -> np.ndarray:
    """Blend road (green) and vehicle (red) masks over the original frame."""
    orig_h, orig_w = frame_bgr.shape[:2]
    mh, mw = roads_mask.shape

    if (mh, mw) != (orig_h, orig_w):
        roads_mask = cv2.resize(roads_mask, (orig_w, orig_h), interpolation=cv2.INTER_NEAREST)
        vehicles_mask = cv2.resize(vehicles_mask, (orig_w, orig_h), interpolation=cv2.INTER_NEAREST)

    out = frame_bgr.astype(np.float32)

    road_px = roads_mask.astype(bool)
    out[road_px] = (1 - alpha) * out[road_px] + alpha * _ROAD_BGR

    veh_px = vehicles_mask.astype(bool)
    out[veh_px] = (1 - alpha) * out[veh_px] + alpha * _VEHICLE_BGR

    return out.clip(0, 255).astype(np.uint8)


def _process_video(
    video_path: Path,
    output_path: Path,
    sess: OnnxSegmentationSession,
    suppress_classes: list[int] | None,
    alpha: float,
) -> None:
    # Resolve model input H, W — dimensions may be symbolic strings for dynamic axes.
    shape = sess.input_shape  # [batch, C, H, W]
    model_h = shape[2] if isinstance(shape[2], int) else None
    model_w = shape[3] if isinstance(shape[3], int) else None

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"[WARN] Cannot open: {video_path}", file=sys.stderr)
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Fall back to original resolution if model dims are dynamic
    target_h = model_h or orig_h
    target_w = model_w or orig_w

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (orig_w, orig_h))

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        inp = _preprocess(frame, (target_h, target_w))
        raw = sess.run(inp)
        roads_mask, vehicles_mask = _postprocess(raw, suppress_classes)
        annotated = _annotate(frame, roads_mask, vehicles_mask, alpha)
        writer.write(annotated)

        frame_idx += 1
        if frame_idx % 50 == 0:
            pct = 100.0 * frame_idx / total if total > 0 else 0.0
            print(f"  frame {frame_idx}/{total}  ({pct:.0f}%)", flush=True)

    cap.release()
    writer.release()
    print(f"  → {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Annotate MP4 videos with road/vehicle segmentation masks."
    )
    parser.add_argument("videos", nargs="+", help="Input MP4 file paths")
    parser.add_argument(
        "--model",
        default="checkpoints/segmentation_1280_720.onnx",
        help="Path to ONNX model checkpoint",
    )
    parser.add_argument("--output-dir", required=True, help="Directory to write annotated videos")
    parser.add_argument(
        "--alpha", type=float, default=0.45, help="Mask overlay opacity (default: 0.45)"
    )
    parser.add_argument(
        "--suppress-classes",
        nargs="*",
        type=int,
        default=None,
        help="Class indices to remap to background before decoding",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with OnnxSegmentationSession(args.model) as sess:
        print(f"Model : '{sess.input_name}' {sess.input_shape}")

        for video_str in args.videos:
            video_path = Path(video_str)
            if not video_path.exists():
                print(f"[WARN] Not found: {video_path}", file=sys.stderr)
                continue
            output_path = out_dir / f"{video_path.stem}_annotated.mp4"
            print(f"\n{video_path.name}")
            _process_video(video_path, output_path, sess, args.suppress_classes, args.alpha)

    print("\nDone.")


if __name__ == "__main__":
    main()
