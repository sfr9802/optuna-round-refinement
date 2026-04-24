"""Offline / CI stub for the auto-loop's --llm-cmd and --llm-cmd-final.

This script exists so the auto-loop can be exercised in CI and in the
README demo without a real LLM API call. A production user would
replace `--llm-cmd 'python stub_llm.py per-round ...'` with something
like `--llm-cmd 'claude -p < {llm_input} > {next_config}'` or a
codex-cli invocation.

The stub is deliberately minimal:

- `per-round`: read the round's bundle.json (structured) instead of
  parsing markdown. Emit a schema-valid next_round_config.json that
  carries the search space forward unchanged. The skill mechanically
  overrides round_id and provenance.{source_round_id, source_bundle_hash,
  parent_config_hash, kind}, so the stub only needs to keep the
  scientific fields shaped right.

- `final`: read the trajectory markdown and emit a one-screen
  placeholder final report so summary.md has something to link to.

Both modes use only stdlib so they can run anywhere optuna runs.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _per_round(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle)
    bundle: Dict[str, Any] = json.loads(bundle_path.read_text(encoding="utf-8"))

    search_space = bundle.get("search_space") or {}
    if not search_space:
        print("stub_llm: bundle has empty search_space — cannot continue", file=sys.stderr)
        return 1

    optuna_meta = bundle.get("optuna") or {}
    sampler = optuna_meta.get("sampler") or {"type": "TPESampler", "params": {}}
    pruner = optuna_meta.get("pruner") or {"type": "MedianPruner", "params": {}}
    sampler.setdefault("params", {})
    pruner.setdefault("params", {})

    next_cfg: Dict[str, Any] = {
        "schema_version": "1.0",
        # round_id will be overwritten by the skill — placeholder value.
        "round_id": "round_99",
        "n_trials": int(bundle.get("n_trials") or 8),
        "direction": (bundle.get("objective") or {}).get("direction", "maximize"),
        "objective_name": (bundle.get("objective") or {}).get("name", "objective"),
        "sampler": sampler,
        "pruner": pruner,
        "search_space": search_space,
        "fixed_params": dict(bundle.get("fixed_params") or {}),
        "provenance": {
            # All four mechanical fields below are overwritten by the
            # skill (round_id, source_round_id, source_bundle_hash,
            # parent_config_hash, kind, generated_at). The stub fills
            # them with placeholders so a strict pre-validation pass
            # would still see a well-shaped object.
            "kind": "llm_proposed",
            "source_round_id": "round_99",
            "source_bundle_hash": "0" * 64,
            "parent_config_hash": "0" * 64,
            "generated_at": _now_iso(),
            "generated_by": {
                "tool": "stub_llm",
                "model": None,
                "prompt_version": "stub-0.1.0",
                "prompt_path": "examples/auto_loop/stub_llm.py",
            },
            "rationale": (
                "Stub LLM: carry the search space forward unchanged. "
                "Replace --llm-cmd with a real model invocation to get "
                "an actual analyst proposal."
            ),
            "diff_summary": [],
        },
        "notes": "stub_llm per-round output (no real analysis performed).",
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(next_cfg, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"stub_llm: wrote per-round config -> {out_path}")
    return 0


def _final(args: argparse.Namespace) -> int:
    in_path = Path(args.input)
    body = in_path.read_text(encoding="utf-8") if in_path.exists() else ""

    report = (
        "# Final study review (stub)\n\n"
        "_This report was produced by `examples/auto_loop/stub_llm.py`._\n"
        "_Replace `--llm-cmd-final` with a real model invocation for a real review._\n\n"
        "## 1. Trajectory bytes received\n\n"
        f"- Input file: `{in_path}`\n"
        f"- Length: {len(body)} chars\n\n"
        "## 2. Stub conclusion\n\n"
        "No analysis performed. The stub exists so the auto-loop can be exercised\n"
        "end-to-end in CI without a real LLM call.\n"
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"stub_llm: wrote final report -> {out_path}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="stub_llm",
        description=(
            "Offline stub for optuna-round-refinement's auto-loop. "
            "Emits schema-valid placeholder outputs so the loop can be "
            "tested without calling a real LLM."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_pr = sub.add_parser(
        "per-round",
        help="Emit a next-round config from the round's bundle.",
    )
    p_pr.add_argument("--bundle", required=True, help="Path to the round's bundle.json")
    p_pr.add_argument("--out", required=True, help="Path to write next_config.json")
    p_pr.set_defaults(func=_per_round)

    p_f = sub.add_parser(
        "final",
        help="Emit a final report from the trajectory markdown.",
    )
    p_f.add_argument("--in", dest="input", required=True, help="Path to trajectory.md")
    p_f.add_argument("--out", required=True, help="Path to write final_report.md")
    p_f.set_defaults(func=_final)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
