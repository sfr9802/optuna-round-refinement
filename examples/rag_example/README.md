# RAG example — round 01 → round 02 walkthrough

A minimal end-to-end example of the round-refinement workflow for a
retrieval-augmented generation (RAG) study. This is the currently
validated example for the package; the `../tabular_toy/` directory is
illustrative only.

> **Identifiers have been generalised for public release.** The study
> id (`rag_quality_round_01`) and objective name (`rag_answer_f1`) are
> placeholder identifiers for the RAG workflow that produced these
> artifacts; any project-specific dataset, corpus, or domain names have
> intentionally been kept out of the checked-in example so it can be
> published without domain leakage. Substitute your own identifiers when
> adapting the example.

## Files (in reading order)

1. [`round_01_bundle.json`](round_01_bundle.json) — what the skill's
   runner wrote after round 1's 40 Optuna trials finished. This is the
   machine input to the LLM analyst.
2. [`round_01_llm_input.md`](round_01_llm_input.md) — how that bundle is
   rendered by the skill's canonical renderer
   [`../../scripts/round_adapter.py::render_llm_input`](../../scripts/round_adapter.py)
   (which fills [`../../templates/llm_input.md`](../../templates/llm_input.md)
   AND resolves the coverage-note column inside the package). This is the
   human-readable input to the LLM analyst.
3. [`round_01_analysis.md`](round_01_analysis.md) — the round report the LLM
   emitted, filled from [`../../templates/round_report.md`](../../templates/round_report.md).
4. [`round_02_config.json`](round_02_config.json) — a **materialised,
   schema-valid** next-round config. Validates cleanly against
   [`../../schemas/next_round_config.schema.json`](../../schemas/next_round_config.schema.json).
   - `provenance.source_bundle_hash` is the real sha256 of
     `round_01_bundle.json` in its canonical form
     (`json.dumps(..., sort_keys=True, separators=(",",":"))`).
   - `provenance.parent_config_hash` is a deterministic demonstrative
     sha256 (computed over a fixed placeholder string) — the repo does
     not ship the round-01 initial config separately.
   - Operator-set fields (`evaluate`, `direction`, `objective_name`,
     `study_id`) are *not* populated in this example because the
     walkthrough predates their introduction; in a live setup the LLM
     MUST carry those forward from the parent config unchanged so the
     runner can re-invoke the same `evaluate` callable in the next
     round.
   - `reviewer.kind == "human"` with a populated `approved_at`
     timestamp, demonstrating what a signed-off config looks like.
5. [`round_02_config.template.json`](round_02_config.template.json) —
   the same config **before adapter materialisation**, retained as the
   LLM-authored template. Contains `"__FILL_AT_ADAPTER__"` sentinels
   for `source_bundle_hash` / `parent_config_hash`, which the adapter
   replaces with real sha256 digests at write time. This file is NOT
   schema-valid on its own and is not expected to validate; it exists
   to illustrate what the LLM emits before the skill-owned runner
   hashes and signs the artefact.

> **Placeholder policy.** Checked-in `*.template.json` files MAY contain
> the `"__FILL_AT_ADAPTER__"` sentinel. Checked-in `*.json` files that
> are not marked `.template.json` are expected to validate cleanly
> against the schemas. See [`../../SKILL.md`](../../SKILL.md) §6 and
> [`../../docs/design.md`](../../docs/design.md) §4 for the hash
> provenance chain.

## What a reader should take away

- The LLM never saw raw RAG training data — only the bundle.
- Every change in `round_02_config.json` cites a specific bundle field.
- The config is declarative JSON, not Python. Optuna code stays in the
  skill-owned [`scripts/round_runner.py`](../../scripts/round_runner.py)
  — no project adapter is written.
- Coverage enrichment (`statistics.axis_coverage`) and the per-param
  coverage note come from the skill's canonical entry points
  (`build_study_bundle` / `render_llm_input` in
  [`../../scripts/round_adapter.py`](../../scripts/round_adapter.py));
  downstream projects do not author coverage logic or template helpers.
- Round 01 did **not** have a bundle feeding it — it was the initial round,
  so in practice a `round_01_config.json` with `provenance.kind == "initial"`
  would live next to these files. It is omitted here to keep the example
  focused on the R → R+1 transition.
