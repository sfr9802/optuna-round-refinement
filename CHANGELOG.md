# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.2.0

### Fixed
- Fixed an ambiguity where `statistics.boundary_hits.<p>.high = 0`
  could mean either "weak sampled boundary" or "unsampled boundary".
- Added internally generated `statistics.axis_coverage` to distinguish
  the sampled range from the configured range on every numeric param.
- Prevented narrow recommendations from relying on `boundary_hits`
  alone — the NARROW guardrails in the proposer prompts now require
  joint citation of `axis_coverage` and the PRUNED/FAIL state of any
  cited boundary trials (see `docs/anti_patterns.md#a10`).
- Added conservative handling for legacy bundles without coverage
  information: loaders do not fabricate `axis_coverage` from a
  possibly-partial `trials` list, and the rendered bundle surfaces
  "coverage unknown" so the LLM cannot regress to the pre-fix
  behaviour.

### Changed
- Canonical bundle and rendering flow now runs through skill-owned
  entry points in `scripts/round_adapter.py`:
  - `build_study_bundle(raw, out_path=…, validate=True)` — constructs
    + normalises + validates + (optionally) writes a fresh bundle.
  - `load_study_bundle(path, recompute=False)` — reads + safe-normalises
    (tops up coverage notes without stomping trusted coverage values).
  - `write_study_bundle(bundle, out_path)` — normalise + validate + write.
  - `normalize_study_bundle(bundle)` — safe top-up used internally.
  - `render_llm_input(bundle, out_path=…)` — fills
    `templates/llm_input.md` AND resolves the coverage-note column
    inside the package.
- Coverage notes are generated inside the package and rendered as a
  plain `{{this.note}}` field — no custom template/Handlebars helper
  on the downstream side.
- Added an optional `note` string field to each
  `statistics.axis_coverage.<p>` entry in the bundle schema.
- Updated prompts (Claude Code + Codex, analyze + propose), templates,
  examples, and design/anti-pattern docs to label unsampled edges
  explicitly.
- Split the RAG example config into two artefacts to clarify the
  placeholder policy:
  - `examples/rag_example/round_02_config.json` — schema-valid
    materialised example with real sha256 provenance hashes.
  - `examples/rag_example/round_02_config.template.json` — LLM-authored
    form retaining `"__FILL_AT_ADAPTER__"` sentinels (not expected to
    validate).

### Compatibility
- `statistics.boundary_hits` remains unchanged on the wire.
- Legacy bundles without `statistics.axis_coverage` remain schema-valid
  and are treated conservatively ("coverage unknown") end-to-end.
- Downstream projects do **not** need custom adapters or template
  helpers to pick up the safer boundary handling — upgrading this
  package is sufficient. The v0.1.0 `inject_axis_coverage` helper is
  retained for backward compatibility and now also populates the
  per-param `note` field automatically.

## v0.1.0

- Initial public release: schemas, prompts, templates, docs, and the
  RAG/runtime tuning example. See
  [`.claude/release_notes_v0.1.0.md`](.claude/release_notes_v0.1.0.md)
  for details.
