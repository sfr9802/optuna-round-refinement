"""Tests for the skill's canonical bundle entry points in
``scripts/round_adapter.py``, plus the schema/template/prompt guarantees
that depend on them.

The tests are organised around two properties we must preserve:

A. **Zero downstream code changes required.** Callers that invoke the
   package's canonical entry points — ``build_study_bundle``,
   ``load_study_bundle``, ``normalize_study_bundle``,
   ``render_llm_input``, or the backward-compatible ``inject_axis_coverage``
   — get axis_coverage AND the per-param ``note`` field automatically.
   No test assumes a downstream adapter must call
   ``inject_axis_coverage`` manually on top of its existing flow.

B. **Legacy-safe.** A bundle written before this release (no
   ``axis_coverage``) still validates against the schema, still renders
   through the shipped template, and is classified as "coverage
   unknown" by the prompts. Loading such a bundle via the canonical
   loader MUST NOT fabricate coverage from a possibly-partial
   ``trials`` list.

The tests are deliberately std-lib + jsonschema only, so they run in any
environment that already satisfies SKILL.md's runtime deps.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

# Make the skill package importable when running `python -m unittest`
# from the repo root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.round_adapter import (
    build_study_bundle,
    compute_axis_coverage,
    inject_axis_coverage,
    load_study_bundle,
    normalize_study_bundle,
    render_llm_input,
    write_study_bundle,
)

_SCHEMA = json.loads((_ROOT / "schemas" / "study_bundle.schema.json").read_text(encoding="utf-8"))


def _trial(number: int, param_value: float, state: str = "COMPLETE", value: float = 0.5) -> dict:
    return {
        "number": number,
        "state": state,
        "value": None if state != "COMPLETE" else value,
        "params": {"x": param_value},
    }


def _minimal_bundle(trials, space, *, extra_stats=None) -> dict:
    """Build a schema-valid bundle around the given trials/search_space."""
    stats = {
        "n_complete": sum(1 for t in trials if t["state"] == "COMPLETE"),
        "n_pruned":   sum(1 for t in trials if t["state"] == "PRUNED"),
        "n_failed":   sum(1 for t in trials if t["state"] == "FAIL"),
    }
    if extra_stats:
        stats.update(extra_stats)
    best = next(
        (t for t in trials if t["state"] == "COMPLETE"),
        trials[0] if trials else None,
    )
    return {
        "schema_version": "1.0",
        "round_id": "round_01",
        "study_id": "test_study",
        "optuna": {
            "version": "3.6.1",
            "sampler": {"type": "TPESampler", "params": {}, "seed": 0},
            "pruner":  {"type": "NopPruner",  "params": {}},
        },
        "objective": {"name": "metric", "direction": "maximize"},
        "search_space": space,
        "n_trials": len(trials),
        "trials": trials,
        "best_trial": best,
        "statistics": stats,
    }


class TestComputeAxisCoverage(unittest.TestCase):
    """Case 1: unsampled upper edge. Case 2: sampled-but-poor upper edge.

    The helper itself is model-agnostic; it just needs to report the
    sampled range, unique count, AND the human-readable ``note`` so that
    the template/prompts can classify the edge without extra helper
    logic on the consumer side.
    """

    def test_unsampled_upper_edge(self):
        # Configured [3, 15]; completes sampled [3..14]; upper edge 15
        # was never sampled. boundary_hits.high = 0 is ambiguous without
        # this coverage signal.
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8, 9, 10, 11, 12, 13, 14])]
        cov = compute_axis_coverage(trials, space)
        self.assertIn("x", cov)
        self.assertEqual(cov["x"]["sampled_min"], 3)
        self.assertEqual(cov["x"]["sampled_max"], 14)
        self.assertEqual(cov["x"]["unique_count"], 10)
        # The gap the analyst must see:
        self.assertLess(cov["x"]["sampled_max"], space["x"]["high"])
        self.assertEqual(cov["x"]["sampled_min"], space["x"]["low"])
        # The package — not the adapter — owns the coverage note string.
        self.assertEqual(cov["x"]["note"], "upper edge UNSAMPLED")

    def test_sampled_but_poor_upper_edge(self):
        # Configured [3, 15]; completes include 15 multiple times.
        # Under the NARROW guardrails this is allowed to justify narrow.
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [
            _trial(0, 5),  _trial(1, 7),  _trial(2, 8),
            _trial(3, 15), _trial(4, 15), _trial(5, 15),
            _trial(6, 9),  _trial(7, 10),
        ]
        cov = compute_axis_coverage(trials, space)
        self.assertEqual(cov["x"]["sampled_max"], 15)  # upper edge reached
        self.assertGreaterEqual(cov["x"]["unique_count"], 2)
        # Lower edge 3 never reached (smallest is 5), upper edge reached.
        self.assertEqual(cov["x"]["note"], "lower edge UNSAMPLED")

    def test_both_edges_unsampled_emits_joint_note(self):
        space = {"x": {"type": "int", "low": 1, "high": 20}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8, 10, 12, 14])]
        cov = compute_axis_coverage(trials, space)
        self.assertEqual(
            cov["x"]["note"], "lower edge UNSAMPLED; upper edge UNSAMPLED"
        )

    def test_full_coverage_note(self):
        # Sampled values span both configured edges exactly.
        space = {"x": {"type": "int", "low": 1, "high": 4}}
        trials = [_trial(i, v) for i, v in enumerate([1, 2, 3, 4])]
        cov = compute_axis_coverage(trials, space)
        self.assertEqual(cov["x"]["note"], "full coverage")

    def test_float_param_type_preserves_floats(self):
        space = {"x": {"type": "float", "low": 0.0, "high": 1.0, "log": False}}
        trials = [_trial(i, v) for i, v in enumerate([0.1, 0.25, 0.5, 0.75, 0.9])]
        cov = compute_axis_coverage(trials, space)
        self.assertAlmostEqual(cov["x"]["sampled_min"], 0.1)
        self.assertAlmostEqual(cov["x"]["sampled_max"], 0.9)
        self.assertIsInstance(cov["x"]["sampled_min"], float)
        self.assertEqual(cov["x"]["unique_count"], 5)
        self.assertEqual(
            cov["x"]["note"], "lower edge UNSAMPLED; upper edge UNSAMPLED"
        )

    def test_empty_completes_yields_nulls_and_unknown_note(self):
        # All pruned/failed → no valid completes → sampled_*_=null, unique=0.
        space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
        trials = [
            _trial(0, 0.2, state="PRUNED", value=0.0),
            _trial(1, 0.7, state="FAIL",   value=0.0),
        ]
        for t in trials:
            t["value"] = None
        cov = compute_axis_coverage(trials, space)
        self.assertIsNone(cov["x"]["sampled_min"])
        self.assertIsNone(cov["x"]["sampled_max"])
        self.assertEqual(cov["x"]["unique_count"], 0)
        self.assertEqual(
            cov["x"]["note"],
            "no valid completes — coverage unknown for this axis",
        )

    def test_categorical_params_are_skipped(self):
        space = {
            "x": {"type": "int", "low": 1, "high": 10},
            "flag": {"type": "categorical", "choices": ["a", "b", "c"]},
        }
        trials = [
            {
                "number": i,
                "state": "COMPLETE",
                "value": 0.5,
                "params": {"x": v, "flag": "a"},
            }
            for i, v in enumerate([1, 2, 3])
        ]
        cov = compute_axis_coverage(trials, space)
        self.assertIn("x", cov)
        self.assertNotIn("flag", cov)

    def test_pruned_trials_are_ignored_for_coverage(self):
        # Only COMPLETE trials contribute; a PRUNED trial at x=15 should
        # NOT push sampled_max to 15. This is deliberate — the helper
        # reports "coverage with evaluable objective values" so that the
        # analyst knows when a configured edge was attempted but never
        # successfully completed.
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [
            _trial(0, 5),
            _trial(1, 8),
            _trial(2, 15, state="PRUNED", value=0.0),
        ]
        trials[2]["value"] = None
        cov = compute_axis_coverage(trials, space)
        self.assertEqual(cov["x"]["sampled_max"], 8)


class TestInjectAxisCoverage(unittest.TestCase):
    """Backward-compatible helper; v0.1.0 callers who use it continue to
    work, and now also get the per-param ``note`` field injected
    automatically (no extra call on the adapter side)."""

    def test_populates_statistics_axis_coverage(self):
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8, 9, 10, 11, 12, 13, 14])]
        bundle = _minimal_bundle(trials, space)
        inject_axis_coverage(bundle)
        self.assertIn("axis_coverage", bundle["statistics"])
        self.assertEqual(
            bundle["statistics"]["axis_coverage"]["x"]["sampled_max"], 14
        )
        # The v0.1.0 helper now also populates the coverage note, so
        # downstream adapters that were ALREADY calling it pick up the
        # safer rendering automatically.
        self.assertEqual(
            bundle["statistics"]["axis_coverage"]["x"]["note"],
            "upper edge UNSAMPLED",
        )

    def test_idempotent(self):
        space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
        trials = [_trial(i, v) for i, v in enumerate([0.2, 0.3, 0.4])]
        bundle = _minimal_bundle(trials, space)
        inject_axis_coverage(bundle)
        first = dict(bundle["statistics"]["axis_coverage"]["x"])
        inject_axis_coverage(bundle)
        self.assertEqual(bundle["statistics"]["axis_coverage"]["x"], first)


class TestBuildStudyBundleIsCanonical(unittest.TestCase):
    """The canonical bundle constructor owns every safety-critical
    enrichment. Downstream adapters pass a raw dict; nothing else is
    required from them.

    These tests intentionally assemble a bundle dict WITHOUT calling
    ``inject_axis_coverage`` or touching ``axis_coverage`` directly, and
    assert that ``build_study_bundle`` produces a fully-enriched,
    schema-valid bundle.
    """

    def test_auto_injects_axis_coverage_and_note(self):
        space = {"x": {"type": "int", "low": 1, "high": 20}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8, 10])]
        raw = _minimal_bundle(trials, space)
        self.assertNotIn("axis_coverage", raw["statistics"])  # no manual call

        result = build_study_bundle(raw)

        # Axis coverage injected without a helper call on the adapter side.
        self.assertIn("axis_coverage", result["statistics"])
        entry = result["statistics"]["axis_coverage"]["x"]
        self.assertEqual(entry["sampled_min"], 3)
        self.assertEqual(entry["sampled_max"], 10)
        # Coverage note resolved inside the package.
        self.assertEqual(
            entry["note"], "lower edge UNSAMPLED; upper edge UNSAMPLED"
        )

    def test_validates_against_schema_by_default(self):
        space = {"x": {"type": "int", "low": 1, "high": 10}}
        trials = [_trial(i, v) for i, v in enumerate([1, 5, 10])]
        raw = _minimal_bundle(trials, space)
        # Must not raise.
        build_study_bundle(raw)

    def test_writes_bundle_to_disk_when_out_path_given(self):
        space = {"x": {"type": "int", "low": 1, "high": 10}}
        trials = [_trial(i, v) for i, v in enumerate([1, 5, 10])]
        raw = _minimal_bundle(trials, space)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bundle.json"
            build_study_bundle(raw, out_path=out)
            self.assertTrue(out.exists())
            on_disk = json.loads(out.read_text(encoding="utf-8"))
            # Coverage + note are baked into the written JSON, so a
            # downstream reader sees the safety enrichment without
            # invoking any skill helper.
            self.assertIn("axis_coverage", on_disk["statistics"])
            self.assertIn("note", on_disk["statistics"]["axis_coverage"]["x"])


class TestWriteStudyBundleIsCanonical(unittest.TestCase):
    def test_normalises_and_writes(self):
        space = {"x": {"type": "int", "low": 1, "high": 10}}
        trials = [_trial(i, v) for i, v in enumerate([1, 5, 10])]
        bundle = _minimal_bundle(trials, space)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bundle.json"
            write_study_bundle(bundle, out)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("axis_coverage", written["statistics"])
            self.assertIn("note", written["statistics"]["axis_coverage"]["x"])


class TestNormalizeStudyBundle(unittest.TestCase):
    """``normalize_study_bundle`` is the single, authoritative
    safe-normalisation step every canonical path funnels through.

    If a bundle already carries trusted coverage values (from a fresh
    build or a hand-crafted example), the normaliser MUST preserve them
    and only top up the note. If coverage is absent, the default path
    leaves it absent (legacy-safe) so the template renders "coverage
    unknown" — see A10.
    """

    def test_adds_note_to_preexisting_axis_coverage_without_stomping(self):
        space = {"x": {"type": "int", "low": 1, "high": 20}}
        bundle = _minimal_bundle([_trial(0, 5)], space)
        # Trusted axis_coverage — possibly hand-crafted with partial trials.
        bundle["statistics"]["axis_coverage"] = {
            "x": {"sampled_min": 3, "sampled_max": 14, "unique_count": 10}
        }
        normalize_study_bundle(bundle)
        entry = bundle["statistics"]["axis_coverage"]["x"]
        self.assertEqual(entry["sampled_min"], 3)  # preserved
        self.assertEqual(entry["sampled_max"], 14)  # preserved
        self.assertEqual(entry["unique_count"], 10)  # preserved
        # Note added by the skill.
        self.assertEqual(
            entry["note"], "lower edge UNSAMPLED; upper edge UNSAMPLED"
        )

    def test_legacy_bundle_stays_legacy_by_default(self):
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7])]
        bundle = _minimal_bundle(trials, space)
        # No axis_coverage present; default normalize MUST NOT fabricate
        # it — the trials list on disk may only be a partial top-k.
        normalize_study_bundle(bundle)
        self.assertNotIn("axis_coverage", bundle["statistics"])


class TestLoadStudyBundleIsCanonical(unittest.TestCase):
    def test_loads_and_tops_up_notes_without_stomping(self):
        space = {"x": {"type": "int", "low": 1, "high": 20}}
        bundle = _minimal_bundle([_trial(0, 5)], space)
        bundle["statistics"]["axis_coverage"] = {
            "x": {"sampled_min": 3, "sampled_max": 14, "unique_count": 10}
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.json"
            p.write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )
            loaded = load_study_bundle(p)
        self.assertEqual(
            loaded["statistics"]["axis_coverage"]["x"]["sampled_max"], 14
        )
        self.assertEqual(
            loaded["statistics"]["axis_coverage"]["x"]["note"],
            "lower edge UNSAMPLED; upper edge UNSAMPLED",
        )

    def test_loads_legacy_bundle_without_axis_coverage_safely(self):
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7])]
        bundle = _minimal_bundle(trials, space)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "b.json"
            p.write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )
            loaded = load_study_bundle(p)
        # Legacy-safe: axis_coverage stays absent, so the template and
        # prompts render "coverage unknown" rather than fabricating data
        # from a possibly-partial trials list.
        self.assertNotIn("axis_coverage", loaded["statistics"])


class TestRenderLlmInputOwnsCoverageNote(unittest.TestCase):
    """The package's canonical renderer resolves the coverage-note column
    without any downstream helper. Legacy bundles fall back to the
    "coverage unknown" legend automatically.
    """

    def test_renders_coverage_note_column_from_package(self):
        space = {"x": {"type": "int", "low": 1, "high": 20}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 10])]
        bundle = build_study_bundle(_minimal_bundle(trials, space))
        rendered = render_llm_input(bundle)
        # Table column is resolved inside the package — the template's
        # old {{coverage_note this @key}} helper is gone and is NOT
        # required on the adapter side.
        self.assertIn("Coverage note", rendered)
        self.assertIn("lower edge UNSAMPLED; upper edge UNSAMPLED", rendered)
        self.assertNotIn("{{coverage_note", rendered)
        self.assertNotIn("{{this.note", rendered)

    def test_legacy_bundle_renders_coverage_unknown_safely(self):
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7])]
        bundle = _minimal_bundle(
            trials,
            space,
            extra_stats={"boundary_hits": {"x": {"low": 1, "high": 0}}},
        )
        # Explicitly NOT calling any helper — the renderer must handle
        # a legacy bundle on its own.
        rendered = render_llm_input(bundle)
        self.assertIn("coverage is **unknown**", rendered)
        # Narrow MUST NOT be justified by boundary_hits alone for legacy bundles.
        self.assertIn("boundary_hits", rendered)

    def test_renders_coverage_note_even_if_note_field_missing(self):
        # A bundle that has axis_coverage but no 'note' — the renderer
        # must still produce the coverage-note column without a helper
        # on the adapter side.
        space = {"x": {"type": "int", "low": 1, "high": 20}}
        bundle = _minimal_bundle([_trial(0, 5)], space)
        bundle["statistics"]["axis_coverage"] = {
            "x": {"sampled_min": 3, "sampled_max": 14, "unique_count": 10}
        }
        rendered = render_llm_input(bundle)
        self.assertIn("lower edge UNSAMPLED; upper edge UNSAMPLED", rendered)


class TestSchemaContract(unittest.TestCase):
    """The schema itself must accept both new and legacy bundles.

    Legacy = no axis_coverage at all. This is the "safe fallback" case
    the skill must preserve.
    """

    def _validate(self, bundle):
        jsonschema.validate(bundle, _SCHEMA)

    def test_bundle_with_axis_coverage_validates(self):
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8, 9, 10, 11, 12, 13, 14])]
        bundle = _minimal_bundle(
            trials,
            space,
            extra_stats={"boundary_hits": {"x": {"low": 1, "high": 0}}},
        )
        inject_axis_coverage(bundle)
        self._validate(bundle)
        # ``note`` is part of the per-entry shape and must be schema-valid.
        self.assertIn("note", bundle["statistics"]["axis_coverage"]["x"])

    def test_bundle_with_note_field_is_accepted(self):
        # A bundle that already carries a 'note' must validate — this is
        # what the canonical build path writes to disk.
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8])]
        bundle = _minimal_bundle(trials, space)
        bundle["statistics"]["axis_coverage"] = {
            "x": {
                "sampled_min": 3,
                "sampled_max": 8,
                "unique_count": 4,
                "note": "upper edge UNSAMPLED",
            }
        }
        self._validate(bundle)

    def test_legacy_bundle_without_axis_coverage_still_validates(self):
        space = {"x": {"type": "int", "low": 3, "high": 15}}
        trials = [_trial(i, v) for i, v in enumerate([3, 5, 7, 8, 9, 10, 11, 12, 13, 14])]
        bundle = _minimal_bundle(
            trials,
            space,
            extra_stats={"boundary_hits": {"x": {"low": 1, "high": 0}}},
        )
        # Explicitly do NOT call inject_axis_coverage.
        self.assertNotIn("axis_coverage", bundle["statistics"])
        self._validate(bundle)  # must still validate

    def test_null_sampled_range_accepted(self):
        space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
        trials = [_trial(0, 0.5, state="PRUNED", value=0.0)]
        trials[0]["value"] = None
        bundle = _minimal_bundle(trials, space)
        inject_axis_coverage(bundle)
        self.assertIsNone(bundle["statistics"]["axis_coverage"]["x"]["sampled_min"])
        self._validate(bundle)

    def test_shipped_example_bundles_validate(self):
        # Both checked-in example bundles must validate under the new schema.
        for path in [
            _ROOT / "examples" / "tabular_toy" / "study_bundle.json",
            _ROOT / "examples" / "rag_example" / "round_01_bundle.json",
        ]:
            with self.subTest(bundle=str(path)):
                bundle = json.loads(path.read_text(encoding="utf-8"))
                self._validate(bundle)

    def test_shipped_example_bundles_have_baked_coverage_notes(self):
        # Adopters should see the coverage-note field already baked into
        # the published examples so the skill's ownership of coverage
        # rendering is obvious — no downstream renderer work required.
        for path in [
            _ROOT / "examples" / "tabular_toy" / "study_bundle.json",
            _ROOT / "examples" / "rag_example" / "round_01_bundle.json",
        ]:
            with self.subTest(bundle=str(path)):
                bundle = json.loads(path.read_text(encoding="utf-8"))
                cov = bundle.get("statistics", {}).get("axis_coverage", {})
                self.assertTrue(cov, msg=f"axis_coverage missing in {path}")
                for name, entry in cov.items():
                    self.assertIn(
                        "note", entry,
                        msg=f"axis_coverage.{name}.note missing in {path}",
                    )

    def test_shipped_final_config_examples_validate(self):
        """Checked-in non-template config examples must validate against
        ``schemas/next_round_config.schema.json``.

        A ``*.template.json`` sibling is allowed to carry
        ``__FILL_AT_ADAPTER__`` sentinels and is intentionally NOT
        expected to validate — see the separate test below.
        """
        config_schema = json.loads(
            (_ROOT / "schemas" / "next_round_config.schema.json").read_text(
                encoding="utf-8"
            )
        )
        finals = sorted(
            p for p in (_ROOT / "examples").rglob("*_config.json")
            if not p.name.endswith(".template.json")
        )
        self.assertTrue(finals, "expected at least one checked-in *_config.json example")
        for path in finals:
            with self.subTest(config=str(path)):
                cfg = json.loads(path.read_text(encoding="utf-8"))
                jsonschema.validate(cfg, config_schema)

    def test_template_configs_are_clearly_marked(self):
        """Placeholder-bearing template configs MUST live under a
        ``*.template.json`` name, so that a reader/validator cannot
        mistake them for final schema-valid outputs.

        This test locks in the placeholder policy documented in the
        README and in ``examples/rag_example/README.md``: no non-template
        `*_config.json` file is allowed to carry the
        ``__FILL_AT_ADAPTER__`` sentinel in the provenance-hash fields
        that must validate against the sha256 pattern. (Mentions of the
        sentinel inside free-text ``notes`` / ``rationale`` are fine —
        those fields are plain strings.)
        """
        SENTINEL = "__FILL_AT_ADAPTER__"
        hash_fields = ("source_bundle_hash", "parent_config_hash")
        offenders = []
        for path in (_ROOT / "examples").rglob("*_config.json"):
            if path.name.endswith(".template.json"):
                continue
            cfg = json.loads(path.read_text(encoding="utf-8"))
            prov = cfg.get("provenance") or {}
            for field in hash_fields:
                if prov.get(field) == SENTINEL:
                    offenders.append(f"{path}::provenance.{field}")
        self.assertFalse(
            offenders,
            "final *_config.json examples must not carry the "
            f"{SENTINEL!r} sentinel in provenance hashes; move them "
            f"under *.template.json. Offenders: {offenders}",
        )


class TestTemplateAndPromptGuardrails(unittest.TestCase):
    """The template + prompts must contain the UNSAMPLED EDGE / coverage-
    unknown language. These are text checks, not behavioural tests, but
    they are the load-bearing contract that prevents an LLM from
    regressing to the pre-fix behaviour.
    """

    def _read(self, rel):
        return (_ROOT / rel).read_text(encoding="utf-8")

    def test_llm_input_template_calls_out_unsampled_and_legacy(self):
        body = self._read("templates/llm_input.md")
        self.assertIn("UNSAMPLED", body)
        self.assertIn("axis_coverage", body)
        self.assertIn("coverage is **unknown**", body)
        self.assertIn("Never narrow against an UNSAMPLED EDGE", body)

    def test_llm_input_template_consumes_prebaked_note_not_custom_helper(self):
        body = self._read("templates/llm_input.md")
        # The template MUST reference the pre-baked note field from the
        # bundle — not a downstream ``coverage_note`` helper invocation.
        self.assertIn("{{this.note}}", body)
        self.assertNotIn("{{coverage_note ", body)
        self.assertNotIn("{{ coverage_note ", body)

    def test_analyze_prompt_states_safety_rules(self):
        for rel in ["prompts/claude_code/analyze_round.md",
                    "prompts/codex/analyze_round.md"]:
            with self.subTest(prompt=rel):
                body = self._read(rel)
                self.assertIn("UNSAMPLED EDGE", body)
                self.assertIn("axis_coverage", body)
                self.assertIn("lack of evidence", body)
                self.assertIn("coverage unknown", body)

    def test_propose_prompt_has_narrow_guardrails(self):
        for rel in ["prompts/claude_code/propose_next_round.md",
                    "prompts/codex/propose_next_round.md"]:
            with self.subTest(prompt=rel):
                body = self._read(rel)
                self.assertIn("NARROW guardrails", body) \
                    if "claude_code" in rel else self.assertIn("NARROW guardrails", body)
                # Both prompts must contain the hard rule verbatim.
                lower = body.lower()
                self.assertIn("never narrow against an unsampled boundary", lower)
                self.assertIn("random-sampler exploration", lower) \
                    if "claude_code" in rel else self.assertIn("randomsampler", lower.replace(" ", ""))
                self.assertIn("axis_coverage", body)

    def test_anti_patterns_includes_a10(self):
        body = self._read("docs/anti_patterns.md")
        self.assertIn("A10", body)
        self.assertIn("unsampled boundary", body.lower())
        # summary table entry
        self.assertRegex(body, r"\|\s*A10\s*\|")

    def test_anti_patterns_says_skill_owns_coverage(self):
        body = self._read("docs/anti_patterns.md")
        # Downstream adapters do NOT compute coverage; the skill does.
        self.assertIn("canonical entry points", body)
        self.assertIn("do NOT compute coverage", body)

    def test_design_doc_uses_canonical_build_path(self):
        body = self._read("docs/design.md")
        self.assertIn("Re-open", body)
        self.assertIn("axis_coverage", body)
        self.assertIn("UNSAMPLED EDGE", body)
        # The adapter pattern MUST route through the package entry point,
        # not a manual inject_axis_coverage call.
        self.assertIn("build_study_bundle", body)
        self.assertNotRegex(
            body,
            r"inject_axis_coverage\s*\(\s*bundle\s*\)",
            "design.md must not recommend manual inject_axis_coverage calls",
        )

    def test_skill_md_describes_canonical_entry_points(self):
        body = self._read("SKILL.md")
        # The skill must advertise its canonical, auto-enriching entry
        # points so adopters are not told to author coverage logic.
        for name in (
            "build_study_bundle",
            "load_study_bundle",
            "render_llm_input",
        ):
            self.assertIn(name, body)

    def test_readme_points_to_canonical_renderer(self):
        body = self._read("README.md")
        self.assertIn("build_study_bundle", body)
        self.assertIn("render_llm_input", body)


class TestNoDownstreamManualInjectionRequired(unittest.TestCase):
    """Global audit: no test (and no doc/example) should tell a downstream
    user to call ``inject_axis_coverage`` by hand after building a
    bundle. Using the canonical build path MUST be sufficient.
    """

    _FORBIDDEN_EXAMPLE_PATTERNS = (
        "from scripts.round_adapter import inject_axis_coverage",
        # Adapters adopting the skill must not be told to call
        # inject_axis_coverage manually; build_study_bundle owns that.
    )

    def test_example_adapter_uses_canonical_entry_point(self):
        body = (_ROOT / "examples" / "tabular_toy" / "train_eval.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("build_study_bundle", body)
        for pattern in self._FORBIDDEN_EXAMPLE_PATTERNS:
            self.assertNotIn(pattern, body)


if __name__ == "__main__":
    unittest.main()
