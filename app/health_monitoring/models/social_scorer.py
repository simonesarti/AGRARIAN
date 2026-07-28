"""
Online social anomaly scorer using diagonal Mahalanobis distance.
Adapted from agrarian_vision_ad.

Maintains an EMA of per-dimension mean and variance over herd feature vectors
and scores individual tracks by their distance from the herd distribution.
"""
from __future__ import annotations

import numpy as np

from app.health_monitoring.inference.config import AnomalyConfig


class SocialScorer:
    def __init__(self, cfg: AnomalyConfig, feature_dim: int) -> None:
        self._alpha = cfg.social_ema_alpha
        self._min_updates = cfg.social_min_updates
        self._min_herd = cfg.social_min_herd
        self._dim = feature_dim
        self._mean: np.ndarray | None = None
        self._var: np.ndarray | None = None
        self._n_updates = 0

    def update(self, features: np.ndarray) -> None:
        """Update herd statistics from all currently visible tracks. features: (N, F)."""
        if len(features) < self._min_herd:
            return
        batch_mean = features.mean(axis=0)
        batch_var = features.var(axis=0) + 1e-8
        if self._mean is None:
            self._mean = batch_mean.copy()
            self._var = batch_var.copy()
        else:
            self._mean = (1 - self._alpha) * self._mean + self._alpha * batch_mean
            self._var = (1 - self._alpha) * self._var + self._alpha * batch_var
        self._n_updates += 1

    def score(self, feature: np.ndarray) -> float:
        """Diagonal Mahalanobis distance of one track from the herd mean. Returns 0 until warmed up."""
        if self._n_updates < self._min_updates or self._mean is None:
            return 0.0
        diff = feature - self._mean
        return float(np.sqrt(np.sum(diff ** 2 / self._var)))

    def reset(self) -> None:
        self._mean = None
        self._var = None
        self._n_updates = 0
