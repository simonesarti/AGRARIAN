"""
LSTM Autoencoder for animal trajectory reconstruction.
Adapted from agrarian_vision_ad.

Architecture
------------
Encoder: Bidirectional LSTM → concat last fwd+bwd hidden → Linear → Tanh bottleneck
Decoder: latent → Linear → (h0, c0) seed → LSTM with zero input → Linear proj

Training target: velocity features only (indices 0-5).
Spatial context features (6-8: rel_x, rel_y, avg_knn_dist) feed the encoder
but are not reconstructed — their anomaly signal is covered by the social scorer.
"""
from __future__ import annotations

import torch
import torch.nn as nn

from src.health_monitoring.inference.config import ModelConfig

# Velocity features reconstructed by the decoder.
# Indices: 0 vx_world, 1 vy_world, 2 rel_vx, 3 rel_vy, 4 speed, 5 rel_speed
VEL_IDX: tuple[int, ...] = (0, 1, 2, 3, 4, 5)


class _Encoder(nn.Module):
    def __init__(self, feature_dim: int, hidden_dim: int, num_layers: int, latent_dim: int, dropout: float) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=feature_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.bottleneck = nn.Sequential(nn.Linear(hidden_dim * 2, latent_dim), nn.Tanh())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, (h_n, _) = self.lstm(x)
        h = torch.cat([h_n[-2], h_n[-1]], dim=-1)
        return self.bottleneck(h)


class _Decoder(nn.Module):
    def __init__(self, feature_dim: int, hidden_dim: int, num_layers: int, latent_dim: int, dropout: float) -> None:
        super().__init__()
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.feature_dim = feature_dim
        self.latent_to_state = nn.Linear(latent_dim, 2 * num_layers * hidden_dim)
        self.lstm = nn.LSTM(
            input_size=feature_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.proj = nn.Linear(hidden_dim, feature_dim)

    def _init_state(self, z: torch.Tensor):
        B = z.size(0)
        states = self.latent_to_state(z)
        h0, c0 = states.chunk(2, dim=-1)
        h0 = h0.view(B, self.num_layers, self.hidden_dim).permute(1, 0, 2).contiguous()
        c0 = c0.view(B, self.num_layers, self.hidden_dim).permute(1, 0, 2).contiguous()
        return h0, c0

    def forward(self, z: torch.Tensor, seq_len: int) -> torch.Tensor:
        h0, c0 = self._init_state(z)
        B = z.size(0)
        inp = z.new_zeros(B, seq_len, self.feature_dim)
        out, _ = self.lstm(inp, (h0, c0))
        return self.proj(out)


class TrajectoryAutoEncoder(nn.Module):
    def __init__(self, cfg: ModelConfig) -> None:
        super().__init__()
        self.encoder = _Encoder(cfg.feature_dim, cfg.hidden_dim, cfg.num_layers, cfg.latent_dim, cfg.dropout)
        self.decoder = _Decoder(len(VEL_IDX), cfg.hidden_dim, cfg.num_layers, cfg.latent_dim, cfg.dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (B, T, feature_dim) → x_hat: (B, T, len(VEL_IDX))"""
        return self.decoder(self.encoder(x), x.size(1))

    @torch.no_grad()
    def reconstruction_error(self, x: torch.Tensor) -> torch.Tensor:
        """Per-sample MSE on velocity features only. Returns (B,) tensor."""
        x_hat = self(x)
        n = len(VEL_IDX)
        return ((x[:, :, :n] - x_hat) ** 2).mean(dim=(1, 2))
