"""Smoke tests for the auto-loop subcommand.

These tests exercise the full N-round flow with a no-op evaluate
callable and the offline stub LLM (`examples/auto_loop/stub_llm.py`).
They cover:

- The mechanical-field overrides (round_id increment, provenance
  source_round_id / source_bundle_hash / parent_config_hash, evaluate
  carryover, kind = "llm_proposed") happen exactly once per round and
  produce a schema-valid next config.
- The trajectory + summary + final_report files are all produced.
- The hard cap on --rounds is enforced.
- A failing --llm-cmd causes a clean RuntimeError after the configured
  retry count, with the partial run preserved.

The tests are stdlib + jsonschema + optuna only. They do NOT call any
real LLM API.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import jsonschema

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.round_runner import (  # noqa: E402
    AUTO_LOOP_HARD_CAP,
    _next_round_id,
    _sha256_canonical,
    run_auto_loop,
)


_NEXT_CONFIG_SCHEMA = json.loads(
    (_ROOT / "schemas" / "next_round_config.schema.json").read_text(encoding="utf-8")
)


def _write_dummy_evaluate(dir_path: Path) -> None:
    """Write a deterministic evaluate.py that returns a function of params.

    Kept intentionally trivial so each round completes in milliseconds.
    """
    (dir_path / "dummy_eval.py").write_text(
        textwrap.dedent(
            """\
            def evaluate(params):
                # Reward larger x and smaller y; deterministic.
                x = float(params.get("x", 0.0))
                y = float(params.get("y", 0.0))
                return {"primary": x - 0.5 * y, "secondary": {"x": x, "y": y}}
            """
        ),
        encoding="utf-8",
    )


def _initial_config(round_id: str = "round_01") -> dict:
    return {
        "schema_version": "1.0",
        "round_id": round_id,
        "n_trials": 4,
        "evaluate": "dummy_eval:evaluate",
        "direction": "maximize",
        "objective_name": "score",
        "study_id": "auto_loop_test",
        "sampler": {"type": "TPESampler", "params": {"n_startup_trials": 2}, "seed": 7},
        "pruner": {"type": "NopPruner", "params": {}},
        "search_space": {
            "x": {"type": "float", "low": 0.0, "high": 1.0},
            "y": {"type": "float", "low": 0.0, "high": 1.0},
        },
        "fixed_params": {},
        "provenance": {
            "kind": "initial",
            "source_round_id": None,
            "source_bundle_hash": None,
            "parent_config_hash": None,
            "generated_at": "2026-04-24T00:00:00Z",
            "generated_by": {"tool": "test"},
            "rationale": "smoke test initial round",
        },
    }


def _stub_cmd_per_round() -> str:
    """Per-round LLM stub invocation suitable for --llm-cmd."""
    stub = _ROOT / "examples" / "auto_loop" / "stub_llm.py"
    return f'"{sys.executable}" "{stub}" per-round --bundle {{bundle}} --out {{next_config}}'


def _stub_cmd_final() -> str:
    stub = _ROOT / "examples" / "auto_loop" / "stub_llm.py"
    return f'"{sys.executable}" "{stub}" final --in {{trajectory}} --out {{final_report}}'


class AutoLoopSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="auto_loop_test_"))
        _write_dummy_evaluate(self.tmp)
        self.cfg_path = self.tmp / "initial.json"
        self.cfg_path.write_text(
            json.dumps(_initial_config(), indent=2), encoding="utf-8"
        )
        self.out_dir = self.tmp / "out"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_three_round_run_produces_full_artifact_tree(self) -> None:
        result = run_auto_loop(
            self.cfg_path,
            rounds=3,
            llm_cmd=_stub_cmd_per_round(),
            llm_cmd_final=_stub_cmd_final(),
            out_dir=self.out_dir,
            evaluate_search_path=self.tmp,
        )

        self.assertEqual(result["rounds_completed"], 3)
        for r in (1, 2, 3):
            rd = self.out_dir / f"round_{r:02d}"
            self.assertTrue((rd / "config.json").exists(), f"missing config.json for round {r}")
            self.assertTrue((rd / "bundle.json").exists(), f"missing bundle.json for round {r}")
            self.assertTrue((rd / "llm_input.md").exists(), f"missing llm_input.md for round {r}")
        # next_config exists for all but the last round
        self.assertTrue((self.out_dir / "round_01" / "next_config.json").exists())
        self.assertTrue((self.out_dir / "round_02" / "next_config.json").exists())
        self.assertFalse((self.out_dir / "round_03" / "next_config.json").exists())

        self.assertTrue((self.out_dir / "trajectory.md").exists())
        self.assertTrue((self.out_dir / "summary.md").exists())
        self.assertTrue((self.out_dir / "final_report.md").exists())

    def test_next_configs_are_schema_valid_with_correct_provenance(self) -> None:
        run_auto_loop(
            self.cfg_path,
            rounds=3,
            llm_cmd=_stub_cmd_per_round(),
            llm_cmd_final=None,  # no final call; trajectory still rendered
            out_dir=self.out_dir,
            evaluate_search_path=self.tmp,
        )

        # Round 1 -> Round 2 transition
        nc2 = json.loads((self.out_dir / "round_01" / "next_config.json").read_text(encoding="utf-8"))
        jsonschema.validate(nc2, _NEXT_CONFIG_SCHEMA)
        self.assertEqual(nc2["round_id"], "round_02")
        self.assertEqual(nc2["evaluate"], "dummy_eval:evaluate")
        self.assertEqual(nc2["provenance"]["kind"], "llm_proposed")
        self.assertEqual(nc2["provenance"]["source_round_id"], "round_01")
        self.assertRegex(nc2["provenance"]["source_bundle_hash"], r"^[a-f0-9]{64}$")
        self.assertRegex(nc2["provenance"]["parent_config_hash"], r"^[a-f0-9]{64}$")

        # Verify the source_bundle_hash actually matches the on-disk bundle
        bundle_disk = json.loads((self.out_dir / "round_01" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(nc2["provenance"]["source_bundle_hash"], _sha256_canonical(bundle_disk))

        # Round 2 -> Round 3 transition
        nc3 = json.loads((self.out_dir / "round_02" / "next_config.json").read_text(encoding="utf-8"))
        jsonschema.validate(nc3, _NEXT_CONFIG_SCHEMA)
        self.assertEqual(nc3["round_id"], "round_03")
        self.assertEqual(nc3["provenance"]["source_round_id"], "round_02")

    def test_rounds_must_be_specified_and_positive(self) -> None:
        with self.assertRaises(ValueError):
            run_auto_loop(
                self.cfg_path,
                rounds=0,
                llm_cmd=_stub_cmd_per_round(),
                out_dir=self.out_dir,
                evaluate_search_path=self.tmp,
            )

    def test_hard_cap_is_enforced(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            run_auto_loop(
                self.cfg_path,
                rounds=AUTO_LOOP_HARD_CAP + 1,
                llm_cmd=_stub_cmd_per_round(),
                out_dir=self.out_dir,
                evaluate_search_path=self.tmp,
            )
        self.assertIn("AUTO_LOOP_HARD_CAP", str(ctx.exception))

    def test_failing_llm_cmd_raises_after_retries_and_preserves_round_one(self) -> None:
        # A command that always fails. Use a python -c that exits non-zero.
        bad_cmd = f'"{sys.executable}" -c "import sys; sys.exit(7)"'
        with self.assertRaises(RuntimeError) as ctx:
            run_auto_loop(
                self.cfg_path,
                rounds=2,
                llm_cmd=bad_cmd,
                out_dir=self.out_dir,
                evaluate_search_path=self.tmp,
                llm_retries=1,
            )
        self.assertIn("LLM command failed", str(ctx.exception))
        # Round 1 artifacts should still be on disk.
        self.assertTrue((self.out_dir / "round_01" / "bundle.json").exists())
        self.assertTrue((self.out_dir / "round_01" / "llm_input.md").exists())

    def test_next_round_id_helper(self) -> None:
        self.assertEqual(_next_round_id("round_01"), "round_02")
        self.assertEqual(_next_round_id("round_09"), "round_10")
        self.assertEqual(_next_round_id("round_99"), "round_100")
        with self.assertRaises(ValueError):
            _next_round_id("not_a_round")


if __name__ == "__main__":
    unittest.main()
