"""
Pydantic config classes for health-monitoring inference.
Adapted from the agrarian_vision_ad project (ego-motion section removed —
BotSORT's internal GMC is used instead).
"""
from __future__ import annotations

from pydantic import BaseModel


class FeaturesConfig(BaseModel):
    # Number of frames of history fed to the autoencoder.  @ 30 fps ≈ 10 s.
    sequence_length: int = 300
    # K topological neighbours used for relative velocity / position features.
    # Scale-invariant: uses animal count, not a pixel radius.
    local_context_k: int = 5
    # Frames a track may be absent before its history is discarded; during the
    # gap the last known feature vector is repeated so scoring is continuous.
    interp_max_age: int = 8


class AnomalyConfig(BaseModel):
    use_ae: bool = True                 # enable LSTM-AE reconstruction scorer
    use_social: bool = True             # enable social Mahalanobis scorer
    ae_threshold: float = 2.75          # AE z-score above which a track is flagged
    social_threshold: float = 5.0       # Mahalanobis distance above which flagged
    smoothing_window: int = 56          # temporal smoothing window (frames)
    min_anomaly_duration: int = 90      # events shorter than this are discarded
    social_ema_alpha: float = 0.007     # EMA decay for online herd statistics
    social_min_updates: int = 375       # frames before social scores are emitted
    social_min_herd: int = 5            # minimum visible animals to update/score
    require_both: bool = False          # AND mode: flag only when both scorers fire


class ModelConfig(BaseModel):
    feature_dim: int = 9
    hidden_dim: int = 128
    latent_dim: int = 64
    num_layers: int = 2
    dropout: float = 0.2
