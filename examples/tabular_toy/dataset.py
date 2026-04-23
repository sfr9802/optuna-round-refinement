"""Tabular dataset loader for the toy PyTorch HPO example.

Uses sklearn's load_breast_cancer for a small, reproducible binary
classification split. Features are standardised on the training split
only; the validation split is transformed with the same scaler.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
from sklearn.datasets import load_breast_cancer
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler


@dataclass(frozen=True)
class TabularSplit:
    X_train: torch.Tensor
    y_train: torch.Tensor
    X_val: torch.Tensor
    y_val: torch.Tensor
    n_features: int


def load_tabular_split(seed: int = 42, val_size: float = 0.2) -> TabularSplit:
    data = load_breast_cancer()
    X = data.data.astype(np.float32)
    y = data.target.astype(np.float32)

    X_tr, X_va, y_tr, y_va = train_test_split(
        X, y, test_size=val_size, stratify=y, random_state=seed
    )
    scaler = StandardScaler().fit(X_tr)
    X_tr = scaler.transform(X_tr).astype(np.float32)
    X_va = scaler.transform(X_va).astype(np.float32)

    return TabularSplit(
        X_train=torch.from_numpy(X_tr),
        y_train=torch.from_numpy(y_tr),
        X_val=torch.from_numpy(X_va),
        y_val=torch.from_numpy(y_va),
        n_features=X_tr.shape[1],
    )
