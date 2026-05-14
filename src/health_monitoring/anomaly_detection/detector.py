"""
Anomaly detector: combines LSTM-AE reconstruction error with social Mahalanobis distance.
Adapted from agrarian_vision_ad.

Each scorer is evaluated independently against its own threshold.
require_both=False: flag if either score exceeds its threshold (default).
require_both=True:  flag only when both scores exceed their thresholds.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
import torch

from src.health_monitoring.inference.config import AnomalyConfig, FeaturesConfig, ModelConfig
from src.health_monitoring.models.social_scorer import SocialScorer
from src.health_monitoring.models.trajectory_ae import TrajectoryAutoEncoder
from src.health_monitoring.tracking.feature_extractor import TrackFeatures


@dataclass
class AnomalyEvent:
    """A contiguous anomalous episode for a single track."""
    track_id: int
    start_frame: int
    end_frame: int
    start_time_ms: float
    end_time_ms: float
    max_ae_score: float
    mean_ae_score: float
    max_social_score: float
    mean_social_score: float
    triggered_by: str   # "ae" | "social" | "both"

    @property
    def duration_ms(self) -> float:
        return self.end_time_ms - self.start_time_ms


@dataclass
class FrameAnomalyResult:
    """Per-frame scoring output.

    Track classification (from most to least severe):
      confirmed_both / confirmed_ae / confirmed_soc  — threshold exceeded AND duration confirmed
      elevated_both  / elevated_ae  / elevated_soc   — threshold exceeded, not yet confirmed
      scored_ok_tracks                               — scored, neither threshold exceeded
      unscored_tracks                                — insufficient history
    """
    frame_idx: int
    timestamp_ms: float
    ae_scores: dict[int, float]
    social_scores: dict[int, float]
    ongoing_events: list[AnomalyEvent]
    unscored_tracks: list[int]
    scored_ok_tracks: list[int]
    ok_ae_tracks: list[int]
    ok_soc_tracks: list[int]
    elevated_ae_tracks: list[int]
    elevated_soc_tracks: list[int]
    elevated_both_tracks: list[int]
    confirmed_ae_tracks: list[int]
    confirmed_soc_tracks: list[int]
    confirmed_both_tracks: list[int]

    @property
    def anomalous_tracks(self) -> list[int]:
        return self.confirmed_ae_tracks + self.confirmed_soc_tracks + self.confirmed_both_tracks


class AnomalyDetector:
    def __init__(
        self,
        anomaly_cfg: AnomalyConfig,
        model_cfg: ModelConfig,
        features_cfg: FeaturesConfig,
        device: str = "cpu",
    ) -> None:
        self.cfg = anomaly_cfg
        self.device = device
        self._ae_seq_len = features_cfg.sequence_length

        self.ae = TrajectoryAutoEncoder(model_cfg).to(device)
        self.social = SocialScorer(anomaly_cfg, model_cfg.feature_dim)

        self._ae_history: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=anomaly_cfg.smoothing_window))
        self._social_history: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=anomaly_cfg.smoothing_window))
        self._active_events: dict[int, Optional[AnomalyEvent]] = {}
        self._completed_events: list[AnomalyEvent] = []

        self._feat_mean: np.ndarray | None = None
        self._feat_std: np.ndarray | None = None
        self._ae_mean: float = 0.0
        self._ae_std: float = 1.0

    def load_weights(self, path: str) -> None:
        ck = torch.load(path, map_location=self.device, weights_only=False)
        self.ae.load_state_dict(ck["ae_state_dict"])
        self._ae_mean = float(ck.get("ae_score_mean", 0.0))
        self._ae_std = float(ck.get("ae_score_std", 1.0))
        self._feat_mean = ck.get("feat_mean", None)
        self._feat_std = ck.get("feat_std", None)

    def set_eval(self) -> None:
        self.ae.eval()

    def score_tracks(
        self,
        track_features: dict[int, TrackFeatures],
        frame_idx: int,
        timestamp_ms: float,
    ) -> FrameAnomalyResult:
        # Purge state for absent tracks (mirrors FeatureExtractor.update()).
        for tid in list(self._ae_history.keys()):
            if tid not in track_features:
                del self._ae_history[tid]
        for tid in list(self._social_history.keys()):
            if tid not in track_features:
                del self._social_history[tid]
        for tid in list(self._active_events.keys()):
            if tid not in track_features:
                self._close_event(tid, frame_idx, timestamp_ms)

        if not track_features:
            return FrameAnomalyResult(
                frame_idx=frame_idx, timestamp_ms=timestamp_ms,
                ae_scores={}, social_scores={}, ongoing_events=[],
                unscored_tracks=[], scored_ok_tracks=[],
                ok_ae_tracks=[], ok_soc_tracks=[],
                elevated_ae_tracks=[], elevated_soc_tracks=[], elevated_both_tracks=[],
                confirmed_ae_tracks=[], confirmed_soc_tracks=[], confirmed_both_tracks=[],
            )

        if self.cfg.use_social:
            latest = np.stack([self._normalize(tf.feature_sequence[-1]) for tf in track_features.values()], axis=0)
            self.social.update(latest)

        ae_scores: dict[int, float] = {}
        social_scores: dict[int, float] = {}

        for tid, tf in track_features.items():
            if len(tf.feature_sequence) < self._ae_seq_len:
                continue
            ae_z = self._ae_z_score(tf) if self.cfg.use_ae else 0.0
            soc = self.social.score(self._normalize(tf.feature_sequence[-1])) if self.cfg.use_social else 0.0
            self._ae_history[tid].append(ae_z)
            self._social_history[tid].append(soc)
            ae_scores[tid] = float(np.mean(self._ae_history[tid]))
            social_scores[tid] = float(np.mean(self._social_history[tid]))

        anomalous: list[int] = []
        for tid in ae_scores:
            ae_flagged = self.cfg.use_ae and ae_scores[tid] > self.cfg.ae_threshold
            soc_flagged = self.cfg.use_social and social_scores[tid] > self.cfg.social_threshold
            if (ae_flagged and soc_flagged) if self.cfg.require_both else (ae_flagged or soc_flagged):
                anomalous.append(tid)
                self._open_or_extend_event(tid, frame_idx, timestamp_ms, ae_scores[tid], social_scores[tid], ae_flagged, soc_flagged)
            else:
                self._close_event(tid, frame_idx, timestamp_ms)

        confirmed: list[int] = [
            tid for tid in anomalous
            if (ev := self._active_events.get(tid)) is not None
            and ev.end_frame - ev.start_frame + 1 >= self.cfg.min_anomaly_duration
        ]

        ae_elevated_set  = {tid for tid in ae_scores if self.cfg.use_ae and ae_scores[tid] > self.cfg.ae_threshold}
        soc_elevated_set = {tid for tid in social_scores if self.cfg.use_social and social_scores[tid] > self.cfg.social_threshold}
        both_elevated = ae_elevated_set & soc_elevated_set
        only_ae       = ae_elevated_set - soc_elevated_set
        only_soc      = soc_elevated_set - ae_elevated_set
        confirmed_set = set(confirmed)

        ongoing = [e for e in self._active_events.values() if e is not None]
        return FrameAnomalyResult(
            frame_idx=frame_idx,
            timestamp_ms=timestamp_ms,
            ae_scores=ae_scores,
            social_scores=social_scores,
            ongoing_events=ongoing,
            unscored_tracks=[tid for tid in track_features if tid not in ae_scores],
            scored_ok_tracks=[tid for tid in ae_scores if tid not in ae_elevated_set and tid not in soc_elevated_set],
            ok_ae_tracks=[tid for tid in ae_scores if tid not in ae_elevated_set],
            ok_soc_tracks=[tid for tid in social_scores if tid not in soc_elevated_set],
            elevated_ae_tracks=[tid for tid in only_ae if tid not in confirmed_set],
            elevated_soc_tracks=[tid for tid in only_soc if tid not in confirmed_set],
            elevated_both_tracks=[tid for tid in both_elevated if tid not in confirmed_set],
            confirmed_ae_tracks=[tid for tid in confirmed if tid in only_ae],
            confirmed_soc_tracks=[tid for tid in confirmed if tid in only_soc],
            confirmed_both_tracks=[tid for tid in confirmed if tid in both_elevated],
        )

    def reset(self) -> None:
        self._ae_history.clear()
        self._social_history.clear()
        self._active_events.clear()
        self._completed_events.clear()
        self.social.reset()

    def _normalize(self, features: np.ndarray) -> np.ndarray:
        if self._feat_mean is None:
            return features
        return (features - self._feat_mean) / self._feat_std

    def _ae_z_score(self, tf: TrackFeatures) -> float:
        seq = torch.from_numpy(self._normalize(tf.feature_sequence)).unsqueeze(0).float().to(self.device)
        error = float(self.ae.reconstruction_error(seq).item())
        return (error - self._ae_mean) / self._ae_std

    def _open_or_extend_event(self, tid, frame_idx, ts, ae_score, social_score, ae_flagged, soc_flagged):
        if self._active_events.get(tid) is None:
            triggered_by = "both" if ae_flagged and soc_flagged else ("ae" if ae_flagged else "social")
            self._active_events[tid] = AnomalyEvent(
                track_id=tid, start_frame=frame_idx, end_frame=frame_idx,
                start_time_ms=ts, end_time_ms=ts,
                max_ae_score=ae_score, mean_ae_score=ae_score,
                max_social_score=social_score, mean_social_score=social_score,
                triggered_by=triggered_by,
            )
        else:
            ev = self._active_events[tid]
            assert ev is not None
            ev.end_frame = frame_idx
            ev.end_time_ms = ts
            ev.max_ae_score = max(ev.max_ae_score, ae_score)
            ev.max_social_score = max(ev.max_social_score, social_score)
            n = ev.end_frame - ev.start_frame + 1
            ev.mean_ae_score += (ae_score - ev.mean_ae_score) / n
            ev.mean_social_score += (social_score - ev.mean_social_score) / n
            if ev.triggered_by != "both" and (
                (ae_flagged and ev.triggered_by == "social") or
                (soc_flagged and ev.triggered_by == "ae")
            ):
                ev.triggered_by = "both"

    def _close_event(self, tid, frame_idx, ts):
        ev = self._active_events.pop(tid, None)
        if ev is None:
            return
        if ev.end_frame - ev.start_frame + 1 >= self.cfg.min_anomaly_duration:
            self._completed_events.append(ev)
