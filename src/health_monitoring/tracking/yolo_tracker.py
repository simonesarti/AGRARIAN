"""
YOLO-native tracker wrapper (BotSORT).
Adapted from agrarian_vision_ad — ego-motion via BotSORT's internal GMC only
(manual ORB/SIFT estimator removed).

Uses model.track() which fuses detection and tracking in a single forward pass.
BotSORT's built-in Global Motion Compensation (GMC) estimates the inter-frame
camera homography, which is exposed after each update() call and used downstream
by the feature extractor to compute world-relative velocities.

Track IDs are monotonically increasing integers assigned by BotSORT.
Animals that leave and re-enter the FoV receive new (higher) IDs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class TrackState:
    track_id: int
    bbox: np.ndarray       # [x1, y1, x2, y2] pixel coordinates
    confidence: float
    class_id: int
    center: np.ndarray = None  # type: ignore[assignment]  — set by __post_init__

    def __post_init__(self) -> None:
        self.center = np.array([
            (self.bbox[0] + self.bbox[2]) / 2,
            (self.bbox[1] + self.bbox[3]) / 2,
        ])


class YOLOTracker:
    """
    Thin wrapper around ultralytics YOLO.track() with BotSORT.

    Call load() once, then update(frame) for every frame.
    Tracker state persists across update() calls (persist=True), so IDs are
    consistent within a continuous stream.  Call reset() to flush state.
    """

    _NON_INFERENCE_KEYS = frozenset({"model_checkpoint"})

    def __init__(self, model_checkpoint: str, track_kwargs: dict) -> None:
        self._model_checkpoint = model_checkpoint
        self._track_kwargs = {k: v for k, v in track_kwargs.items() if k not in self._NON_INFERENCE_KEYS}
        self._model = None
        self._gmc_hook_installed: bool = False
        self._last_gmc_warp: Optional[np.ndarray] = None

    def load(self) -> None:
        from ultralytics import YOLO
        self._model = YOLO(self._model_checkpoint)

    def update(self, frame: np.ndarray) -> tuple[list[TrackState], Optional[np.ndarray]]:
        """
        Run detection + tracking on one frame.

        Returns (tracks, H) where tracks is the list of active TrackState objects
        and H is the 3×3 prev→curr homography from BotSORT's GMC (None if unavailable).
        """
        if self._model is None:
            raise RuntimeError("Call load() before update().")

        results = self._model.track(source=frame, stream=False, persist=True, **self._track_kwargs)

        if not self._gmc_hook_installed:
            self._install_gmc_hook()

        H = self._gmc_homography()

        boxes = results[0].boxes
        if boxes is None or boxes.id is None:
            return [], H

        ids    = boxes.id.cpu().numpy().astype(int)
        bboxes = boxes.xyxy.cpu().numpy()
        confs  = boxes.conf.cpu().numpy()
        clss   = boxes.cls.cpu().numpy().astype(int)

        tracks = [
            TrackState(track_id=int(tid), bbox=bbox.copy(), confidence=float(conf), class_id=int(cls))
            for tid, bbox, conf, cls in zip(ids, bboxes, confs, clss)
        ]
        return tracks, H

    def reset(self) -> None:
        """Flush BotSORT's internal state. Call between unrelated video sequences."""
        if self._model is not None:
            predictor = getattr(self._model, "predictor", None)
            if predictor is not None:
                for t in getattr(predictor, "trackers", []):
                    t.reset()
        self._last_gmc_warp = None

    def _install_gmc_hook(self) -> None:
        try:
            predictor = getattr(self._model, "predictor", None)
            trackers = getattr(predictor, "trackers", [])
            gmc = getattr(trackers[0], "gmc", None) if trackers else None
            if gmc is None:
                self._gmc_hook_installed = True
                return
            original_apply = gmc.apply

            def hooked_apply(img, dets):
                warp = original_apply(img, dets)
                self._last_gmc_warp = warp
                return warp

            gmc.apply = hooked_apply
            self._gmc_hook_installed = True
        except (AttributeError, IndexError):
            self._gmc_hook_installed = True

    def _gmc_homography(self) -> Optional[np.ndarray]:
        warp = self._last_gmc_warp
        if warp is None:
            return None
        warp = np.array(warp, dtype=np.float32)
        if warp.shape == (2, 3):
            H = np.eye(3, dtype=np.float32)
            H[:2, :] = warp
            return H
        if warp.shape == (3, 3):
            return warp
        return None
