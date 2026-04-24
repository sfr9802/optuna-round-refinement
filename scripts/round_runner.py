"""Round-level Optuna orchestration owned by the skill package.

This module lets a project run a full Optuna round from a declarative
YAML config alone. The project's only contribution is a callable
``evaluate(params: dict) -> dict | float`` imported by dotted path from
the config:

    # config.yaml
    evaluate: "my_module:evaluate"

    # my_module.py
    def evaluate(params: dict) -> dict:
        return {"primary": score, "secondary": {...}}

No project-side adapter code is required. This module owns:

- sampler / pruner construction from the config,
- ``trial.suggest_*`` dispatch from ``search_space``,
- study-to-bundle export (trial summaries, stats, boundary hits,
  param importances),
- delegation to ``round_adapter.build_study_bundle`` so ``axis_coverage``
  and the per-param coverage note are baked in automatically.

Why this module is separate from ``round_adapter.py``
-----------------------------------------------------
``round_adapter.py`` intentionally has **no** ``optuna``/``numpy``/
``torch`` imports so it can be unit-tested on pure dict inputs and
reused in non-Optuna contexts. All Optuna-facing logic lives here so
that constraint is preserved.

CLI
---
::

    python scripts/round_runner.py run \\
        --config experiment.active.yaml \\
        --out-bundle run_output/study_bundle.json \\
        --out-llm-input run_output/llm_input.md

Config keys consumed (in addition to what ``next_round_config.schema.json``
already validates):

- ``evaluate``: ``"module.path:callable"`` — required.
- ``direction``: ``"maximize"`` (default) or ``"minimize"``.
- ``objective_name``: string for the bundle's ``objective.name`` field;
  defaults to ``"objective"``.
- ``study_id``: optional study name; defaults to ``"round_<round_id>"``.

All other keys (``sampler``, ``pruner``, ``search_space``, ``fixed_params``,
``n_trials``, ``round_id``, ``notes``, ...) match the config schema.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

import optuna
import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from round_adapter import (  # noqa: E402
    build_study_bundle,
    load_study_bundle,
    render_llm_input,
)

__all__ = [
    "run_round",
    "load_evaluate_callable",
    "main",
]

EvaluateResult = Union[float, int, Mapping[str, Any]]
EvaluateFn = Callable[[Dict[str, Any]], EvaluateResult]


# ---------------------------------------------------------------------------
# Evaluate entrypoint resolution
# ---------------------------------------------------------------------------

def load_evaluate_callable(spec: str) -> EvaluateFn:
    """Resolve ``"module.path:callable_name"`` into the callable.

    Raises ValueError / AttributeError / TypeError with a clear message
    if the spec is malformed or the resolved attribute is not callable.
    """
    if not isinstance(spec, str) or ":" not in spec:
        raise ValueError(
            "evaluate spec must be '<module>:<callable>' "
            f"(e.g. 'my_module:evaluate'); got {spec!r}"
        )
    mod_path, attr = spec.split(":", 1)
    mod_path = mod_path.strip()
    attr = attr.strip()
    if not mod_path or not attr:
        raise ValueError(
            f"evaluate spec has empty module or callable name: {spec!r}"
        )
    try:
        module = importlib.import_module(mod_path)
    except ImportError as exc:
        raise ImportError(
            f"could not import evaluate module {mod_path!r}: {exc}. "
            "If the module lives next to your config, pass "
            "--evaluate-search-path (CLI) or evaluate_search_path "
            "(run_round) pointing at its directory."
        ) from exc
    try:
        fn = getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(
            f"module {mod_path!r} has no attribute {attr!r}"
        ) from exc
    if not callable(fn):
        raise TypeError(
            f"evaluate spec {spec!r} resolved to non-callable "
            f"{type(fn).__name__}"
        )
    return fn


def _coerce_result(result: EvaluateResult) -> Dict[str, Any]:
    """Normalise the evaluate return into ``{"primary", "secondary"?}``.

    Accepts either a bare number (treated as ``primary``) or a mapping
    with at least a ``primary`` key.
    """
    if isinstance(result, bool):  # reject: bool is technically int
        raise TypeError(
            "evaluate() returned a bool; expected a number or "
            "a dict with a 'primary' key"
        )
    if isinstance(result, (int, float)):
        return {"primary": float(result), "secondary": None}
    if isinstance(result, Mapping):
        if "primary" not in result:
            raise KeyError(
                "evaluate() dict return must include a 'primary' key"
            )
        primary = result["primary"]
        if not isinstance(primary, (int, float)) or isinstance(primary, bool):
            raise TypeError(
                f"evaluate() 'primary' must be a number; got "
                f"{type(primary).__name__}"
            )
        return {
            "primary": float(primary),
            "secondary": result.get("secondary"),
        }
    raise TypeError(
        "evaluate() must return a number or a dict with a 'primary' "
        f"key; got {type(result).__name__}"
    )


# ---------------------------------------------------------------------------
# Optuna factories (kept narrow on purpose — extend via params only)
# ---------------------------------------------------------------------------

def _build_sampler(cfg: Mapping[str, Any]) -> optuna.samplers.BaseSampler:
    kind = cfg["type"]
    params = dict(cfg.get("params") or {})
    seed = cfg.get("seed")
    if kind == "TPESampler":
        return optuna.samplers.TPESampler(seed=seed, **params)
    if kind == "RandomSampler":
        return optuna.samplers.RandomSampler(seed=seed, **params)
    if kind == "CmaEsSampler":
        return optuna.samplers.CmaEsSampler(seed=seed, **params)
    if kind == "GridSampler":
        # GridSampler requires a search_space param at construction.
        return optuna.samplers.GridSampler(**params)
    raise ValueError(f"unsupported sampler: {kind!r}")


def _build_pruner(cfg: Mapping[str, Any]) -> optuna.pruners.BasePruner:
    kind = cfg["type"]
    params = dict(cfg.get("params") or {})
    if kind == "MedianPruner":
        return optuna.pruners.MedianPruner(**params)
    if kind == "NopPruner":
        return optuna.pruners.NopPruner()
    if kind == "HyperbandPruner":
        return optuna.pruners.HyperbandPruner(**params)
    if kind == "SuccessiveHalvingPruner":
        return optuna.pruners.SuccessiveHalvingPruner(**params)
    raise ValueError(f"unsupported pruner: {kind!r}")


def _suggest_params(
    trial: optuna.Trial, space: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, spec in space.items():
        ptype = spec["type"]
        if ptype == "categorical":
            out[name] = trial.suggest_categorical(name, list(spec["choices"]))
        elif ptype == "int":
            step = spec.get("step") or 1
            out[name] = trial.suggest_int(
                name, spec["low"], spec["high"], step=step
            )
        elif ptype == "float":
            out[name] = trial.suggest_float(
                name,
                spec["low"],
                spec["high"],
                log=bool(spec.get("log", False)),
            )
        else:
            raise ValueError(f"unknown param type: {ptype!r}")
    return out


# ---------------------------------------------------------------------------
# Study → raw bundle (dict) export
# ---------------------------------------------------------------------------

def _trial_summary(t: optuna.trial.FrozenTrial) -> Dict[str, Any]:
    return {
        "number": t.number,
        "state": t.state.name,
        "value": None if t.value is None else float(t.value),
        "params": dict(t.params),
        "user_attrs": dict(t.user_attrs or {}),
    }


def _boundary_hits(
    trials: List[Dict[str, Any]],
    space: Mapping[str, Mapping[str, Any]],
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


def _export_raw_bundle(study: optuna.Study, cfg: Mapping[str, Any]) -> Dict[str, Any]:
    import numpy as np  # lazy import so the module import is cheap

    trials = [_trial_summary(t) for t in study.trials]
    completes = [
        t["value"] for t in trials
        if t["state"] == "COMPLETE" and t["value"] is not None
    ]
    direction = cfg.get("direction", "maximize")
    best_value: Optional[float]
    if not completes:
        best_value = None
    elif direction == "maximize":
        best_value = max(completes)
    else:
        best_value = min(completes)

    stats: Dict[str, Any] = {
        "n_complete": sum(1 for t in trials if t["state"] == "COMPLETE"),
        "n_pruned":   sum(1 for t in trials if t["state"] == "PRUNED"),
        "n_failed":   sum(1 for t in trials if t["state"] == "FAIL"),
        "best_value": best_value,
        "median_value": float(np.median(completes)) if completes else None,
        "mean_value":   float(np.mean(completes))   if completes else None,
        "std_value":    float(np.std(completes))    if completes else None,
    }
    boundary = _boundary_hits(trials, cfg["search_space"])
    if boundary:
        stats["boundary_hits"] = boundary

    raw: Dict[str, Any] = {
        "schema_version": "1.0",
        "round_id": cfg["round_id"],
        "study_id": study.study_name,
        "parent_config_hash": cfg.get("parent_config_hash"),
        "optuna": {
            "version": optuna.__version__,
            "sampler": cfg["sampler"],
            "pruner":  cfg["pruner"],
        },
        "objective": {
            "name": cfg.get("objective_name", "objective"),
            "direction": direction,
        },
        "search_space": dict(cfg["search_space"]),
        "fixed_params": dict(cfg.get("fixed_params") or {}),
        "n_trials": int(cfg["n_trials"]),
        "trials": trials,
        "best_trial": _trial_summary(study.best_trial),
        "statistics": stats,
    }
    if cfg.get("notes"):
        raw["notes"] = cfg["notes"]
    importances = _param_importances(study)
    if importances:
        raw["param_importances"] = importances
    return raw


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _resolve_config(config: Union[str, Path, Mapping[str, Any]]) -> Dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)
    return yaml.safe_load(Path(config).read_text(encoding="utf-8"))


def run_round(
    config: Union[str, Path, Mapping[str, Any]],
    *,
    out_bundle: Optional[Union[str, Path]] = None,
    out_llm_input: Optional[Union[str, Path]] = None,
    evaluate_search_path: Optional[Union[str, Path]] = None,
    evaluate: Optional[EvaluateFn] = None,
) -> Dict[str, Any]:
    """Run one Optuna round and return the normalised study bundle.

    Parameters
    ----------
    config:
        YAML/JSON path or in-memory dict matching
        ``schemas/next_round_config.schema.json`` plus an ``evaluate``
        key pointing at the project's evaluate callable by dotted
        ``module:function`` path.
    out_bundle:
        Where to write the bundle JSON. Skipped when None (caller gets
        the dict only).
    out_llm_input:
        Where to write the rendered LLM-input markdown. Skipped when
        None.
    evaluate_search_path:
        Directory prepended to ``sys.path`` before importing the
        evaluate module. Defaults to the config file's parent directory
        when ``config`` is a path; ignored when ``evaluate`` is passed
        directly or when ``config`` is already a dict.
    evaluate:
        Optional pre-resolved evaluate callable. When given, the
        ``evaluate`` key in the config is not required.
    """
    cfg = _resolve_config(config)

    if evaluate is None:
        if evaluate_search_path is None and not isinstance(config, Mapping):
            evaluate_search_path = Path(config).resolve().parent
        if evaluate_search_path is not None:
            sp = str(Path(evaluate_search_path).resolve())
            if sp not in sys.path:
                sys.path.insert(0, sp)
        spec = cfg.get("evaluate")
        if not spec:
            raise ValueError(
                "config is missing 'evaluate' — add a dotted-path pointer "
                "to your evaluate callable, e.g. evaluate: 'my_module:evaluate'. "
                "Alternatively, pass evaluate=<callable> to run_round()."
            )
        evaluate = load_evaluate_callable(spec)

    fixed_params = dict(cfg.get("fixed_params") or {})
    direction = cfg.get("direction", "maximize")
    study_id = cfg.get("study_id") or f"round_{cfg['round_id']}"

    study = optuna.create_study(
        study_name=study_id,
        direction=direction,
        sampler=_build_sampler(cfg["sampler"]),
        pruner=_build_pruner(cfg["pruner"]),
    )

    def objective(trial: optuna.Trial) -> float:
        sampled = _suggest_params(trial, cfg["search_space"])
        all_params = {**fixed_params, **sampled}
        normalized = _coerce_result(evaluate(all_params))
        if normalized["secondary"] is not None:
            trial.set_user_attr("secondary", normalized["secondary"])
        return normalized["primary"]

    study.optimize(objective, n_trials=int(cfg["n_trials"]))

    raw = _export_raw_bundle(study, cfg)
    bundle = build_study_bundle(
        raw,
        out_path=Path(out_bundle) if out_bundle else None,
    )
    if out_llm_input is not None:
        render_llm_input(bundle, out_path=Path(out_llm_input))
    return bundle


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    bundle = run_round(
        args.config,
        out_bundle=args.out_bundle,
        out_llm_input=args.out_llm_input,
        evaluate_search_path=args.evaluate_search_path,
    )
    stats = bundle.get("statistics") or {}
    msg = (
        f"[optuna-round-refinement] round={bundle.get('round_id')} "
        f"best_value={stats.get('best_value')} "
        f"complete={stats.get('n_complete')} "
        f"pruned={stats.get('n_pruned')} "
        f"failed={stats.get('n_failed')}"
    )
    if args.out_bundle:
        msg += f" bundle={args.out_bundle}"
    if args.out_llm_input:
        msg += f" llm_input={args.out_llm_input}"
    print(msg)
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    bundle = load_study_bundle(args.bundle)
    rendered = render_llm_input(bundle, out_path=args.out)
    if args.out is None:
        sys.stdout.write(rendered)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="optuna-round-refinement",
        description=(
            "Skill-owned orchestration for round-level Optuna HPO with an "
            "LLM-in-the-outer-loop. The project contributes only an "
            "evaluate(params) callable referenced from the config."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser(
        "run",
        help="Run one Optuna round from a config and write the study bundle.",
    )
    p_run.add_argument("--config", type=Path, required=True)
    p_run.add_argument(
        "--out-bundle",
        type=Path,
        default=None,
        help="Where to write the study bundle JSON.",
    )
    p_run.add_argument(
        "--out-llm-input",
        type=Path,
        default=None,
        help="Where to write the rendered LLM-input markdown.",
    )
    p_run.add_argument(
        "--evaluate-search-path",
        type=Path,
        default=None,
        help=(
            "Extra directory prepended to sys.path before importing the "
            "evaluate module. Defaults to the config file's parent directory."
        ),
    )
    p_run.set_defaults(func=_cmd_run)

    p_render = sub.add_parser(
        "render",
        help="Render a saved study bundle as LLM-input markdown.",
    )
    p_render.add_argument("--bundle", type=Path, required=True)
    p_render.add_argument("--out", type=Path, default=None)
    p_render.set_defaults(func=_cmd_render)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
