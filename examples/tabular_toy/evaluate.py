"""Per-trial evaluate callable for the tabular toy HPO example.

This is the **only code a user writes** to integrate with the skill.
Everything else — sampler / pruner construction, ``trial.suggest_*``
dispatch, bundle export, ``axis_coverage`` enrichment, schema
validation, LLM-input rendering — lives in the skill package under
``scripts/round_runner.py`` and ``scripts/round_adapter.py``.

The config (``experiment.active.yaml``) points at this callable via::

    evaluate: "evaluate:evaluate"

When the runner executes, it merges ``fixed_params`` (from the config)
with the search-space values Optuna sampled for this trial, then calls
``evaluate(merged)`` exactly once. The return dict's ``primary`` is the
value Optuna optimises; ``secondary`` is stored on the trial's user
attributes and surfaces in the study bundle.
"""
from __future__ import annotations

import random
import time
from functools import lru_cache
from typing import Any, Dict

import numpy as np
import torch
import torch.nn.functional as F
from sklearn.metrics import roc_auc_score
from torch.utils.data import DataLoader, TensorDataset

from dataset import TabularSplit, load_tabular_split
from model import SimpleMLP


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


@lru_cache(maxsize=4)
def _cached_split(seed: int) -> TabularSplit:
    return load_tabular_split(seed=seed)


def evaluate(params: Dict[str, Any]) -> Dict[str, Any]:
    """Train one MLP and return ``{"primary": val_auc, "secondary": {...}}``.

    ``params`` is the merged dict of search-space values (sampled by
    Optuna) and ``fixed_params`` (from the config). The function does
    not know or care which axes are being searched; it just consumes
    the values it needs.
    """
    seed = int(params.get("seed", 42))
    max_epochs = int(params.get("max_epochs", 20))
    weight_decay = float(params.get("weight_decay", 1e-4))

    _set_seed(seed)
    device = torch.device("cpu")
    data = _cached_split(seed)

    model = SimpleMLP(
        n_features=data.n_features,
        hidden_units=int(params["hidden_units"]),
        num_layers=int(params["num_layers"]),
        dropout=float(params["dropout"]),
        activation=str(params["activation"]),
    ).to(device)

    optim_cls = (
        torch.optim.Adam if params["optimizer"] == "adam"
        else torch.optim.AdamW
    )
    opt = optim_cls(
        model.parameters(),
        lr=float(params["learning_rate"]),
        weight_decay=weight_decay,
    )

    loader = DataLoader(
        TensorDataset(data.X_train, data.y_train),
        batch_size=int(params["batch_size"]),
        shuffle=True,
        drop_last=False,
    )

    t0 = time.perf_counter()
    model.train()
    for _ in range(max_epochs):
        for xb, yb in loader:
            opt.zero_grad(set_to_none=True)
            logits = model(xb.to(device))
            loss = F.binary_cross_entropy_with_logits(logits, yb.to(device))
            loss.backward()
            opt.step()
    train_time_s = time.perf_counter() - t0

    model.eval()
    with torch.no_grad():
        val_logits = model(data.X_val.to(device))
        val_probs = torch.sigmoid(val_logits).cpu().numpy()
    y_true = data.y_val.cpu().numpy()
    val_auc = float(roc_auc_score(y_true, val_probs))
    val_acc = float(((val_probs >= 0.5) == (y_true >= 0.5)).mean())
    n_params = int(sum(p.numel() for p in model.parameters()))

    return {
        "primary": val_auc,
        "secondary": {
            "val_accuracy": round(val_acc, 6),
            "train_time_s": round(train_time_s, 4),
            "n_params": n_params,
        },
    }
