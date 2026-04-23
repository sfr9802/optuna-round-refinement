"""Simple MLP for tabular binary classification."""
from __future__ import annotations

from typing import Literal

import torch
import torch.nn as nn

Activation = Literal["relu", "gelu"]


def _activation(name: Activation) -> nn.Module:
    if name == "relu":
        return nn.ReLU()
    if name == "gelu":
        return nn.GELU()
    raise ValueError(f"unknown activation: {name!r}")


class SimpleMLP(nn.Module):
    def __init__(
        self,
        n_features: int,
        hidden_units: int,
        num_layers: int,
        dropout: float,
        activation: Activation,
    ) -> None:
        super().__init__()
        if num_layers < 1:
            raise ValueError("num_layers must be >= 1")

        blocks: list[nn.Module] = []
        in_dim = n_features
        for _ in range(num_layers):
            blocks += [
                nn.Linear(in_dim, hidden_units),
                _activation(activation),
                nn.Dropout(dropout),
            ]
            in_dim = hidden_units
        blocks.append(nn.Linear(in_dim, 1))
        self.net = nn.Sequential(*blocks)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)
