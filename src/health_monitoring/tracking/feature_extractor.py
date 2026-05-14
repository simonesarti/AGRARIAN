"""
Per-track feature extractor.
Adapted from agrarian_vision_ad.

9-dimensional feature vector per timestep (all normalised by 1/max(W,H)):

  Velocity features (AE reconstruction target, indices 0-5):
    0  vx_world  — ego-motion-compensated x-displacement per frame
    1  vy_world  — ego-motion-compensated y-displacement per frame
    2  rel_vx    — vx relative to mean vx of K nearest neighbours
    3  rel_vy    — vy relative to mean vy of K nearest neighbours
    4  speed     — ‖(vx, vy)‖
    5  rel_speed — speed minus mean speed of K nearest neighbours

  Spatial context features (encoder input only, indices 6-8):
    6  rel_x        — x-position relative to centroid of K nearest neighbours
    7  rel_y        — y-position relative to centroid of K nearest neighbours
    8  avg_knn_dist — mean Euclidean distance to K nearest neighbours

Absolute position is excluded: for a drone those coordinates encode camera
trajectory rather than animal behaviour.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from scipy.spatial.distance import cdist

from src.health_monitoring.inference.config import FeaturesConfig
from src.health_monitoring.tracking.yolo_tracker import TrackState


@dataclass
class TrackFeatures:
    track_id: int
    feature_sequence: np.ndarray   # (T, FEATURE_DIM)


class FeatureExtractor:
    def __init__(self, cfg: FeaturesConfig, frame_w: int, frame_h: int) -> None:
        self.cfg = cfg
        self._fw = frame_w
        self._fh = frame_h
        # track_id → deque of (frame_idx, center_px, feature_vec)
        self._history: dict[int, deque[tuple[int, np.ndarray, np.ndarray]]] = {}
        self._missed: dict[int, int] = {}

    def update(
        self,
        tracks: list[TrackState],
        H_prev_to_curr: Optional[np.ndarray],
        frame_idx: int,
    ) -> dict[int, TrackFeatures]:
        """
        Compute features for all active tracks.

        Short absences (≤ interp_max_age) are bridged by repeating the last known
        feature vector.  Returns a dict {track_id: TrackFeatures} for every active
        and currently-interpolated track.
        """
        active_ids = {t.track_id for t in tracks}

        # Advance interpolated tracks; purge those that exceeded the budget.
        for tid in list(self._history.keys()):
            if tid in active_ids:
                continue
            missed = self._missed.get(tid, 0) + 1
            if missed > self.cfg.interp_max_age:
                del self._history[tid]
                self._missed.pop(tid, None)
            else:
                self._missed[tid] = missed
                _, last_center, last_feat = self._history[tid][-1]
                self._history[tid].append((frame_idx, last_center, last_feat))

        for tid in active_ids:
            self._missed.pop(tid, None)

        if not tracks:
            return {
                tid: TrackFeatures(track_id=tid, feature_sequence=np.stack([s[2] for s in hist], axis=0))
                for tid, hist in self._history.items()
            }

        # World velocities for all active tracks.
        world_vels: dict[int, np.ndarray] = {}
        for t in tracks:
            wv = self._world_velocity(t.track_id, t.center, H_prev_to_curr)
            world_vels[t.track_id] = wv if wv is not None else np.zeros(2)

        N = len(tracks)
        centers = np.array([t.center for t in tracks])
        vel_arr = np.array([world_vels[t.track_id] for t in tracks])
        K = min(self.cfg.local_context_k, N - 1)

        inv_max = 1.0 / max(self._fw, self._fh)
        speed = np.linalg.norm(vel_arr, axis=1)

        if K > 0:
            dist_sq = cdist(centers, centers, metric="sqeuclidean")
            np.fill_diagonal(dist_sq, np.inf)
            k_idx = np.argpartition(dist_sq, K, axis=1)[:, :K]
            local_centroids = centers[k_idx].mean(axis=1)
            local_vels = vel_arr[k_idx].mean(axis=1)
            avg_knn_dist = np.sqrt(dist_sq[np.arange(N)[:, None], k_idx]).mean(axis=1)
            neighbor_speeds = np.linalg.norm(vel_arr[k_idx], axis=2)
            rel_speed = speed - neighbor_speeds.mean(axis=1)
        else:
            local_centroids = centers
            local_vels = vel_arr
            avg_knn_dist = np.zeros(N)
            rel_speed = np.zeros(N)

        feats = (np.column_stack([
            vel_arr[:, 0],
            vel_arr[:, 1],
            vel_arr[:, 0] - local_vels[:, 0],
            vel_arr[:, 1] - local_vels[:, 1],
            speed,
            rel_speed,
            centers[:, 0] - local_centroids[:, 0],
            centers[:, 1] - local_centroids[:, 1],
            avg_knn_dist,
        ]) * inv_max).astype(np.float32)

        result: dict[int, TrackFeatures] = {}
        for i, t in enumerate(tracks):
            if t.track_id not in self._history:
                self._history[t.track_id] = deque(maxlen=self.cfg.sequence_length)
            self._history[t.track_id].append((frame_idx, centers[i], feats[i]))
            seq = self._history[t.track_id]
            result[t.track_id] = TrackFeatures(
                track_id=t.track_id,
                feature_sequence=np.stack([s[2] for s in seq], axis=0),
            )

        for tid, hist in self._history.items():
            if tid not in active_ids:
                result[tid] = TrackFeatures(track_id=tid, feature_sequence=np.stack([s[2] for s in hist], axis=0))

        return result

    def _world_velocity(self, track_id: int, center: np.ndarray, H: Optional[np.ndarray]) -> Optional[np.ndarray]:
        if track_id not in self._history or len(self._history[track_id]) == 0:
            return None
        _, prev_center, _ = self._history[track_id][-1]
        if H is not None:
            prev_warped = cv2.perspectiveTransform(
                prev_center.reshape(1, 1, 2).astype(np.float32), H
            ).reshape(2)
            return center - prev_warped
        return center - prev_center
