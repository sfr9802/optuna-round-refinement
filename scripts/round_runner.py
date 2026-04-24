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
import datetime as _dt
import hashlib
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

import optuna
import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from round_adapter import (  # noqa: E402
    build_study_bundle,
    load_study_bundle,
    render_llm_input,
    render_study_trajectory,
)

__all__ = [
    "run_round",
    "run_auto_loop",
    "load_evaluate_callable",
    "main",
]

# Hard cap on --rounds to prevent runaway studies. The CLI rejects
# anything above this; users with a legitimate need can override via
# AUTO_LOOP_MAX_ROUNDS env var.
AUTO_LOOP_HARD_CAP = 50

_ROUND_ID_RE = re.compile(r"^round_(\d+)$")

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
# Auto-loop: N rounds, LLM-driven transitions, final summary call
# ---------------------------------------------------------------------------

def _next_round_id(current: str) -> str:
    m = _ROUND_ID_RE.match(current)
    if not m:
        raise ValueError(
            f"round_id {current!r} does not match 'round_NN' — cannot increment"
        )
    n = int(m.group(1))
    width = max(len(m.group(1)), 2)
    return f"round_{n + 1:0{width}d}"


def _sha256_canonical(obj: Mapping[str, Any]) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shell_quote(value: str) -> str:
    """Quote a substituted value for the host shell.

    POSIX uses ``shlex.quote`` (single quotes). Windows cmd.exe does not
    honour single quotes, so substituted values are wrapped in double
    quotes with embedded ``"`` escaped as ``""``. This handles typical
    filesystem paths; users with shell metacharacters in paths should
    quote the placeholder explicitly in their template instead.
    """
    if os.name == "nt":
        return '"' + value.replace('"', '""') + '"'
    return shlex.quote(value)


def _render_template(cmd_template: str, mapping: Mapping[str, str]) -> str:
    """Substitute {key} placeholders. Unknown keys are left untouched.

    Path values are quoted via :func:`_shell_quote` so a path with
    spaces stays one token after the host shell parses the command.
    """
    out = cmd_template
    for k, v in mapping.items():
        token = "{" + k + "}"
        if token in out:
            out = out.replace(token, _shell_quote(v))
    return out


def _run_llm_cmd(cmd_template: str, mapping: Mapping[str, str], *, retries: int = 1) -> str:
    """Run the user's LLM command with placeholder substitution.

    Returns the rendered command string for logging. Raises RuntimeError
    on the final retry's failure with stderr captured.
    """
    cmd = _render_template(cmd_template, mapping)
    last_err: Optional[str] = None
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return cmd
        last_err = (
            f"attempt {attempt}/{attempts} failed with exit {proc.returncode}\n"
            f"--- stderr ---\n{proc.stderr or '(empty)'}\n"
            f"--- stdout ---\n{proc.stdout or '(empty)'}"
        )
        if attempt < attempts:
            print(
                f"[optuna-round-refinement] LLM cmd failed "
                f"(attempt {attempt}/{attempts}); retrying once...",
                file=sys.stderr,
            )
    raise RuntimeError(
        f"LLM command failed after {attempts} attempt(s):\n"
        f"  cmd: {cmd}\n{last_err}"
    )


def _load_and_validate_next_config(
    path: Path,
    *,
    expected_round_id: str,
    parent_round_id: str,
    parent_bundle_hash: str,
    parent_config_hash: str,
    evaluate_spec: str,
) -> Dict[str, Any]:
    """Load LLM-produced next config, normalise mechanical fields, validate.

    The skill OWNS the mechanical provenance fields (round_id increment,
    parent hashes, source_round_id, evaluate spec carryover). The LLM
    owns the scientific fields (search_space, sampler, rationale,
    diff_summary). Mechanical-field overrides are silent and idempotent
    so the LLM can be sloppy about them.
    """
    if not path.exists():
        raise RuntimeError(
            f"LLM did not produce next-round config at expected path: {path}\n"
            "Check the --llm-cmd template's {next_config} placeholder."
        )
    raw = path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"LLM-produced next config at {path} is not valid JSON: {exc}\n"
            f"--- file head ---\n{raw[:500]}"
        ) from exc
    if not isinstance(cfg, dict):
        raise RuntimeError(
            f"LLM-produced next config at {path} must be a JSON object; "
            f"got {type(cfg).__name__}"
        )

    cfg["round_id"] = expected_round_id
    cfg.setdefault("schema_version", "1.0")
    if not cfg.get("evaluate"):
        cfg["evaluate"] = evaluate_spec

    prov = cfg.get("provenance")
    if not isinstance(prov, dict):
        prov = {}
        cfg["provenance"] = prov
    prov["kind"] = "llm_proposed"
    prov["source_round_id"] = parent_round_id
    prov["source_bundle_hash"] = parent_bundle_hash
    prov["parent_config_hash"] = parent_config_hash
    if not prov.get("generated_at"):
        prov["generated_at"] = _now_iso()
    gb = prov.get("generated_by")
    if not isinstance(gb, dict):
        prov["generated_by"] = {"tool": "auto_loop"}
    elif "tool" not in gb:
        gb["tool"] = "auto_loop"
    if "rationale" not in prov or not str(prov.get("rationale")).strip():
        raise RuntimeError(
            f"LLM-produced next config at {path} is missing "
            "provenance.rationale (required by schema)."
        )

    try:
        import jsonschema  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "jsonschema>=4 required to validate next-round config"
        ) from exc
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "next_round_config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(cfg, schema)
    except jsonschema.ValidationError as exc:
        raise RuntimeError(
            f"LLM-produced next config at {path} failed schema validation:\n"
            f"  path: {'.'.join(str(p) for p in exc.absolute_path)}\n"
            f"  message: {exc.message}"
        ) from exc
    return cfg


def _write_canonical_json(path: Path, obj: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return _sha256_canonical(obj)


def _summary_markdown(
    bundles: List[Mapping[str, Any]],
    *,
    out_dir: Path,
    final_report_path: Optional[Path],
) -> str:
    from round_adapter import _trajectory_global_best  # type: ignore[attr-defined]

    best_trial, best_round = _trajectory_global_best(bundles)
    lines: List[str] = []
    lines.append(f"# Auto-loop summary — {len(bundles)} rounds")
    lines.append("")
    lines.append(f"- Output dir: `{out_dir}`")
    if final_report_path is not None:
        lines.append(f"- Final LLM report: [`{final_report_path.name}`]({final_report_path.name})")
    if best_trial is not None:
        lines.append(
            f"- **Global best:** value=`{best_trial.get('value')}` "
            f"(round `{best_round}`, trial #{best_trial.get('number')})"
        )
        lines.append("")
        lines.append("## Global best trial")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(best_trial, indent=2, sort_keys=True))
        lines.append("```")
    else:
        lines.append("- **Global best:** _no completed trials across the study_")
    lines.append("")
    lines.append("## Per-round artifacts")
    lines.append("")
    lines.append("| Round | best | bundle | llm_input | analysis | next_config |")
    lines.append("|-------|------|--------|-----------|----------|-------------|")
    for b in bundles:
        rid = b.get("round_id", "")
        stats = b.get("statistics") or {}
        rd = f"{rid}"
        bv = stats.get("best_value")
        files = []
        for fname in ("bundle.json", "llm_input.md", "analysis.md", "next_config.json"):
            p = out_dir / rd / fname
            if p.exists():
                rel = p.relative_to(out_dir).as_posix()
                files.append(f"[`{fname}`]({rel})")
            else:
                files.append("—")
        lines.append(f"| `{rid}` | {bv} | " + " | ".join(files) + " |")
    lines.append("")
    return "\n".join(lines) + "\n"


def run_auto_loop(
    config: Union[str, Path, Mapping[str, Any]],
    *,
    rounds: int,
    llm_cmd: str,
    out_dir: Union[str, Path],
    llm_cmd_final: Optional[str] = None,
    evaluate_search_path: Optional[Union[str, Path]] = None,
    llm_retries: int = 1,
    max_rounds_cap: int = AUTO_LOOP_HARD_CAP,
) -> Dict[str, Any]:
    """Run an N-round auto-loop with LLM-driven round-to-round transitions.

    Per round R (1 <= R <= N):
      1. Run the Optuna round → ``<out_dir>/round_RR/bundle.json`` +
         ``llm_input.md``.
      2. If R < N: invoke ``llm_cmd`` with ``{llm_input}`` /
         ``{next_config}`` placeholders. Validate, mechanically inject
         provenance / round_id, write ``next_config.json``, and use it
         as the input config for round R+1.
      3. If R == N AND llm_cmd_final is given: render the multi-round
         trajectory, invoke ``llm_cmd_final`` with ``{trajectory}`` /
         ``{final_report}`` placeholders, and write ``final_report.md``.

    Always writes ``<out_dir>/summary.md`` at the end.

    The caller chooses N (``rounds``); there is no default — that is a
    deliberate skill design choice. ``rounds`` must be in [1,
    max_rounds_cap].
    """
    if rounds < 1:
        raise ValueError("rounds must be >= 1")
    if rounds > max_rounds_cap:
        raise ValueError(
            f"rounds={rounds} exceeds AUTO_LOOP_HARD_CAP={max_rounds_cap}; "
            "raise the cap explicitly if this is intentional"
        )

    out_dir_p = Path(out_dir).resolve()
    out_dir_p.mkdir(parents=True, exist_ok=True)

    initial_cfg = _resolve_config(config)
    evaluate_spec = initial_cfg.get("evaluate")
    if not evaluate_spec:
        raise ValueError(
            "auto-loop requires an 'evaluate' pointer in the initial config"
        )
    if evaluate_search_path is None and not isinstance(config, Mapping):
        evaluate_search_path = Path(config).resolve().parent

    bundles: List[Dict[str, Any]] = []
    analyses: List[Tuple[str, str]] = []
    current_cfg: Dict[str, Any] = initial_cfg
    current_cfg_path: Optional[Path] = None  # written inside the loop

    for r in range(1, rounds + 1):
        round_id = current_cfg["round_id"]
        round_dir = out_dir_p / round_id
        round_dir.mkdir(parents=True, exist_ok=True)

        # Persist this round's input config so it has a hash on disk.
        current_cfg_path = round_dir / "config.json"
        cfg_hash = _write_canonical_json(current_cfg_path, current_cfg)

        bundle_path = round_dir / "bundle.json"
        llm_input_path = round_dir / "llm_input.md"

        print(
            f"[optuna-round-refinement] === round {r}/{rounds} "
            f"({round_id}) -- running Optuna ==="
        )
        bundle = run_round(
            current_cfg,
            out_bundle=bundle_path,
            out_llm_input=llm_input_path,
            evaluate_search_path=evaluate_search_path,
        )
        # Re-read canonicalised on-disk bundle so the hash matches what
        # downstream tools would compute.
        on_disk_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle_hash = _sha256_canonical(on_disk_bundle)
        bundles.append(bundle)

        if r < rounds:
            next_round_id = _next_round_id(round_id)
            analysis_path = round_dir / "analysis.md"
            next_config_path = round_dir / "next_config.json"
            print(
                f"[optuna-round-refinement] round {r}/{rounds} done "
                f"(best={bundle.get('statistics', {}).get('best_value')}); "
                f"calling LLM for {next_round_id} config..."
            )
            _run_llm_cmd(
                llm_cmd,
                {
                    "llm_input": str(llm_input_path),
                    "bundle": str(bundle_path),
                    "next_config": str(next_config_path),
                    "analysis": str(analysis_path),
                    "round_id": round_id,
                    "next_round_id": next_round_id,
                },
                retries=llm_retries,
            )
            next_cfg = _load_and_validate_next_config(
                next_config_path,
                expected_round_id=next_round_id,
                parent_round_id=round_id,
                parent_bundle_hash=bundle_hash,
                parent_config_hash=cfg_hash,
                evaluate_spec=evaluate_spec,
            )
            # Re-write canonicalised next_config so downstream tools see
            # the mechanically-normalised form (round_id, provenance, etc.).
            _write_canonical_json(next_config_path, next_cfg)
            if analysis_path.exists():
                analyses.append((round_id, analysis_path.read_text(encoding="utf-8")))
            current_cfg = next_cfg
        else:
            print(
                f"[optuna-round-refinement] round {r}/{rounds} done "
                f"(best={bundle.get('statistics', {}).get('best_value')}); "
                f"final round complete."
            )

    final_report_path: Optional[Path] = None
    if llm_cmd_final and bundles:
        trajectory_path = out_dir_p / "trajectory.md"
        render_study_trajectory(
            bundles,
            analyses=analyses or None,
            out_path=trajectory_path,
        )
        final_report_path = out_dir_p / "final_report.md"
        print(
            f"[optuna-round-refinement] === final summary "
            f"({len(bundles)} rounds) -- calling final LLM ==="
        )
        _run_llm_cmd(
            llm_cmd_final,
            {
                "trajectory": str(trajectory_path),
                "final_report": str(final_report_path),
                "out_dir": str(out_dir_p),
            },
            retries=llm_retries,
        )
        if not final_report_path.exists():
            raise RuntimeError(
                f"--llm-cmd-final did not produce {final_report_path}; "
                "check the {final_report} placeholder."
            )
    elif not llm_cmd_final:
        # Still render the trajectory for the human to read directly.
        trajectory_path = out_dir_p / "trajectory.md"
        render_study_trajectory(
            bundles,
            analyses=analyses or None,
            out_path=trajectory_path,
        )

    summary_path = out_dir_p / "summary.md"
    summary_path.write_text(
        _summary_markdown(bundles, out_dir=out_dir_p, final_report_path=final_report_path),
        encoding="utf-8",
    )

    return {
        "rounds_completed": len(bundles),
        "out_dir": str(out_dir_p),
        "summary": str(summary_path),
        "final_report": str(final_report_path) if final_report_path else None,
        "bundles": bundles,
    }


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


def _cmd_auto(args: argparse.Namespace) -> int:
    result = run_auto_loop(
        args.config,
        rounds=args.rounds,
        llm_cmd=args.llm_cmd,
        llm_cmd_final=args.llm_cmd_final,
        out_dir=args.out_dir,
        evaluate_search_path=args.evaluate_search_path,
        llm_retries=args.llm_retries,
    )
    print(
        f"[optuna-round-refinement] auto-loop done: "
        f"rounds={result['rounds_completed']} "
        f"summary={result['summary']}"
        + (f" final_report={result['final_report']}" if result['final_report'] else "")
    )
    return 0


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected positive integer, got {value!r}"
        ) from exc
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"expected positive integer >= 1, got {n}"
        )
    return n


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

    p_auto = sub.add_parser(
        "auto",
        help=(
            "Run N Optuna rounds back-to-back, calling --llm-cmd between "
            "rounds to produce next configs and (optionally) --llm-cmd-final "
            "after the last round for a study-wide review."
        ),
    )
    p_auto.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Initial round config (YAML or JSON).",
    )
    p_auto.add_argument(
        "--rounds",
        type=_positive_int,
        required=True,
        help=(
            "REQUIRED. Number of rounds to run. Capped at "
            f"{AUTO_LOOP_HARD_CAP} by default to prevent runaway studies."
        ),
    )
    p_auto.add_argument(
        "--llm-cmd",
        type=str,
        required=True,
        help=(
            "Shell command template invoked between rounds. Placeholders: "
            "{llm_input} (path to round's llm_input.md), {next_config} "
            "(path where the LLM should write the next-round config JSON), "
            "{analysis} (optional path for the LLM's round_report.md), "
            "{round_id}, {next_round_id}. The skill quotes substituted "
            "paths via shlex.quote, so the template should NOT add its own "
            "quotes around placeholders."
        ),
    )
    p_auto.add_argument(
        "--llm-cmd-final",
        type=str,
        default=None,
        help=(
            "Optional shell command template invoked after the last round. "
            "Placeholders: {trajectory} (multi-round trajectory markdown), "
            "{final_report} (path where the final review markdown should be "
            "written), {out_dir}. When omitted, the trajectory.md is still "
            "rendered for the human to read directly."
        ),
    )
    p_auto.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help=(
            "Directory to write per-round artifacts "
            "(round_NN/{config,bundle,llm_input,analysis,next_config}), "
            "trajectory.md, final_report.md, and summary.md."
        ),
    )
    p_auto.add_argument(
        "--evaluate-search-path",
        type=Path,
        default=None,
        help=(
            "Extra directory prepended to sys.path before importing the "
            "evaluate module. Defaults to the initial config file's parent."
        ),
    )
    p_auto.add_argument(
        "--llm-retries",
        type=int,
        default=1,
        help="Times to retry a failing --llm-cmd / --llm-cmd-final invocation (default 1).",
    )
    p_auto.set_defaults(func=_cmd_auto)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
