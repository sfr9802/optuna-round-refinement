"""Single-trial training/eval plus an Optuna study runner.

Usage:
    python train_eval.py --config experiment.active.yaml \
        --out study_bundle.json

What this script does:
    - Loads a declarative round config (next_round_config.schema.json shape).
    - Runs one Optuna study with the frozen search_space for that round.
    - Writes a study_bundle.json that conforms to study_bundle.schema.json.

The script keeps the objective function deterministic enough for demo use
(numpy/torch seeded) and CPU-friendly (no CUDA assumptions).
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import optuna
import torch
import torch.nn.functional as F
import yaml
from sklearn.metrics import roc_auc_score
from torch.utils.data import DataLoader, TensorDataset

from dataset import TabularSplit, load_tabular_split
from model import SimpleMLP


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def train_and_eval(
    params: Dict[str, Any],
    data: TabularSplit,
    *,
    max_epochs: int,
    weight_decay: float,
    seed: int,
) -> Dict[str, Any]:
    """Train one MLP and return a compact metrics dict.

    Keys:
        primary:   float  — val_auc (to maximize)
        secondary: dict   — val_accuracy, train_time_s, n_params
        state:     str    — "COMPLETE" on success
    """
    set_seed(seed)
    device = torch.device("cpu")

    model = SimpleMLP(
        n_features=data.n_features,
        hidden_units=int(params["hidden_units"]),
        num_layers=int(params["num_layers"]),
        dropout=float(params["dropout"]),
        activation=str(params["activation"]),
    ).to(device)

    optim_cls = torch.optim.Adam if params["optimizer"] == "adam" else torch.optim.AdamW
    opt = optim_cls(
        model.parameters(),
        lr=float(params["learning_rate"]),
        weight_decay=float(weight_decay),
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
        "state": "COMPLETE",
    }


def _suggest_params(trial: optuna.Trial, space: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, spec in space.items():
        ptype = spec["type"]
        if ptype == "categorical":
            out[name] = trial.suggest_categorical(name, spec["choices"])
        elif ptype == "int":
            step = spec.get("step") or 1
            out[name] = trial.suggest_int(name, spec["low"], spec["high"], step=step)
        elif ptype == "float":
            out[name] = trial.suggest_float(
                name, spec["low"], spec["high"], log=bool(spec.get("log", False))
            )
        else:
            raise ValueError(f"unknown param type: {ptype!r}")
    return out


def _build_sampler(cfg: Dict[str, Any]) -> optuna.samplers.BaseSampler:
    kind = cfg["type"]
    params = dict(cfg.get("params") or {})
    seed = cfg.get("seed")
    if kind == "TPESampler":
        return optuna.samplers.TPESampler(seed=seed, **params)
    if kind == "RandomSampler":
        return optuna.samplers.RandomSampler(seed=seed)
    raise ValueError(f"unsupported sampler: {kind!r}")


def _build_pruner(cfg: Dict[str, Any]) -> optuna.pruners.BasePruner:
    kind = cfg["type"]
    params = dict(cfg.get("params") or {})
    if kind == "MedianPruner":
        return optuna.pruners.MedianPruner(**params)
    if kind == "NopPruner":
        return optuna.pruners.NopPruner()
    raise ValueError(f"unsupported pruner: {kind!r}")


def _boundary_hits(
    trials: List[Dict[str, Any]], space: Dict[str, Any]
) -> Dict[str, Dict[str, int]]:
    hits: Dict[str, Dict[str, int]] = {}
    for name, spec in space.items():
        if spec["type"] == "categorical":
            continue
        lo = float(spec["low"])
        hi = float(spec["high"])
        width = max(hi - lo, 1e-12)
        low_n = high_n = 0
        for t in trials:
            if name not in t["params"]:
                continue
            v = float(t["params"][name])
            if abs(v - lo) / width < 1e-3:
                low_n += 1
            if abs(v - hi) / width < 1e-3:
                high_n += 1
        hits[name] = {"low": low_n, "high": high_n}
    return hits


def _param_importances(study: optuna.Study) -> Optional[Dict[str, float]]:
    try:
        raw = optuna.importance.get_param_importances(study)
    except Exception:
        return None
    total = sum(raw.values()) or 1.0
    return {k: round(float(v) / total, 4) for k, v in raw.items()}


def _trial_summary(t: optuna.trial.FrozenTrial) -> Dict[str, Any]:
    return {
        "number": t.number,
        "state": t.state.name,
        "value": None if t.value is None else float(t.value),
        "params": dict(t.params),
        "user_attrs": dict(t.user_attrs or {}),
    }


def export_bundle(study: optuna.Study, cfg: Dict[str, Any]) -> Dict[str, Any]:
    trials = [_trial_summary(t) for t in study.trials]
    values = [t["value"] for t in trials if t["state"] == "COMPLETE" and t["value"] is not None]

    stats: Dict[str, Any] = {
        "n_complete": sum(1 for t in trials if t["state"] == "COMPLETE"),
        "n_pruned": sum(1 for t in trials if t["state"] == "PRUNED"),
        "n_failed": sum(1 for t in trials if t["state"] == "FAIL"),
        "best_value": max(values) if values else None,
        "median_value": float(np.median(values)) if values else None,
        "mean_value": float(np.mean(values)) if values else None,
        "std_value": float(np.std(values)) if values else None,
    }
    boundary = _boundary_hits(trials, cfg["search_space"])
    if boundary:
        stats["boundary_hits"] = boundary

    best = study.best_trial
    bundle: Dict[str, Any] = {
        "schema_version": "1.0",
        "round_id": cfg["round_id"],
        "study_id": study.study_name,
        "parent_config_hash": None,
        "optuna": {
            "version": optuna.__version__,
            "sampler": cfg["sampler"],
            "pruner": cfg["pruner"],
        },
        "objective": {
            "name": cfg.get("objective_name", "val_auc"),
            "direction": "maximize",
        },
        "search_space": cfg["search_space"],
        "fixed_params": cfg.get("fixed_params", {}),
        "n_trials": cfg["n_trials"],
        "trials": trials,
        "best_trial": _trial_summary(best),
        "statistics": stats,
        "notes": "Illustrative PyTorch tabular HPO example. Not a benchmark.",
    }
    importances = _param_importances(study)
    if importances:
        bundle["param_importances"] = importances
    return bundle


def run_study(config_path: Path, out_bundle: Path) -> Dict[str, Any]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    fixed = dict(cfg.get("fixed_params") or {})
    max_epochs = int(fixed.pop("max_epochs", 20))
    weight_decay = float(fixed.pop("weight_decay", 1e-4))
    seed = int(cfg.get("sampler", {}).get("seed") or 42)

    data = load_tabular_split(seed=seed)

    study = optuna.create_study(
        study_name=f"tabular_toy_{cfg['round_id']}",
        direction="maximize",
        sampler=_build_sampler(cfg["sampler"]),
        pruner=_build_pruner(cfg["pruner"]),
    )

    def objective(trial: optuna.Trial) -> float:
        params = _suggest_params(trial, cfg["search_space"])
        params.update(fixed)
        result = train_and_eval(
            params,
            data,
            max_epochs=max_epochs,
            weight_decay=weight_decay,
            seed=seed,
        )
        trial.set_user_attr("secondary", result["secondary"])
        return result["primary"]

    study.optimize(objective, n_trials=int(cfg["n_trials"]))

    bundle = export_bundle(study, cfg)
    out_bundle.parent.mkdir(parents=True, exist_ok=True)
    out_bundle.write_text(
        json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
    )
    return bundle


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("experiment.active.yaml"),
        help="Path to the round's active config (next_round_config schema).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("study_bundle.json"),
        help="Where to write the study bundle for this round.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    bundle = run_study(args.config, args.out)
    print(
        f"[tabular_toy] wrote {args.out} | "
        f"best_value={bundle['statistics']['best_value']} "
        f"over {bundle['n_trials']} trials"
    )
