import numpy as np
from dataclasses import dataclass
from typing import Optional

from src.health_monitoring.anomaly_detection.detector import FrameAnomalyResult


@dataclass
class HMTrackingSlotMetadata:
    """
    Metadata produced by HMTrackingWorker.

    Carries a shared-memory slot reference to the (H, W, 3) BGR frame at
    processing resolution alongside the tracker outputs for this frame.
    The slot is released by the downstream anomaly detection worker after reading.

    tracks: active TrackState objects (bounding boxes, ids, classes).
    H: 3×3 prev→curr homography from BotSORT's GMC, or None if unavailable.
    is_keyframe: False for frames that were skipped by the tracker (passthrough).
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int
    tracks: list               # list[TrackState]
    H: Optional[np.ndarray]    # ego-motion homography, or None
    is_keyframe: bool = True


@dataclass
class HMAnomalySlotMetadata:
    """
    Metadata produced by HMAnomalyDetectionWorker.

    Carries a shared-memory slot reference to the (H, W, 3) BGR frame at
    processing resolution alongside the anomaly scoring results.

    tracks: active TrackState objects, forwarded for bounding-box drawing.
    anomaly_result: FrameAnomalyResult with per-track scores and classifications.
    is_keyframe: False for frames that bypassed inference (no real tracks/scores).
    """
    frame_id: int
    timestamp: float
    original_wh: tuple[int, int]
    slot_index: int
    tracks: list               # list[TrackState]
    anomaly_result: FrameAnomalyResult
    is_keyframe: bool = True
