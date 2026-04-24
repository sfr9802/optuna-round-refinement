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

1. [`round_01_bundle.json`](round_01_bundle.json) — what the adapter exported
   after round 1's 40 Optuna trials finished. This is the machine input to
   the LLM analyst.
2. [`round_01_llm_input.md`](round_01_llm_input.md) — how that bundle is
   rendered by the skill's canonical renderer
   [`../../scripts/round_adapter.py::render_llm_input`](../../scripts/round_adapter.py)
   (which fills [`../../templates/llm_input.md`](../../templates/llm_input.md)
   AND resolves the coverage-note column inside the package). This is the
   human-readable input to the LLM analyst.
3. [`round_01_analysis.md`](round_01_analysis.md) — the round report the LLM
   emitted, filled from [`../../templates/round_report.md`](../../templates/round_report.md).
4. [`round_02_config.json`](round_02_config.json) — the next-round config the
   LLM emitted, validated against
   [`../../schemas/next_round_config.schema.json`](../../schemas/next_round_config.schema.json).
   Note:
   - `provenance.kind == "llm_proposed"`.
   - `source_bundle_hash` and `parent_config_hash` are placeholders; the
     adapter fills them at write time.
   - `reviewer.kind == "human"` because freezing two important params is
     a "large change" per [`../../docs/anti_patterns.md#a7`](../../docs/anti_patterns.md).
     The adapter will refuse to run round 02 until `approved_at` is set.

> **Hashes in this example are placeholders, not verified artifact hashes.**
> Every hash-looking value in the checked-in files — `round_01_bundle.json`,
> `round_01_llm_input.md`, `round_01_analysis.md`, and `round_02_config.json` —
> is either `null`, the sentinel `"__FILL_AT_ADAPTER__"`, or an explicit
> `<PLACEHOLDER …>` marker. They are **not** real sha256 digests of these
> files. Adopters must recompute hashes from their own canonicalised artifacts
> (see [`../../SKILL.md`](../../SKILL.md) §6 and
> [`../../docs/design.md`](../../docs/design.md) §4) before wiring the example
> into any live workflow.

## What a reader should take away

- The LLM never saw raw RAG training data — only the bundle.
- Every change in `round_02_config.json` cites a specific bundle field.
- The config is declarative JSON, not Python. Optuna code stays in the
  project adapter.
- Round 01 did **not** have a bundle feeding it — it was the initial round,
  so in practice a `round_01_config.json` with `provenance.kind == "initial"`
  would live next to these files. It is omitted here to keep the example
  focused on the R → R+1 transition.
