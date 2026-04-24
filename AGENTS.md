# AGENTS.md — optuna-round-refinement

This repository is a portable skill package: **round-level Optuna
hyperparameter refinement with an LLM operating only as the outer-loop
analyst** between Optuna rounds. Codex loads this file automatically when
run inside the repo.

## Start here

- [`SKILL.md`](./SKILL.md) — machine-readable skill manifest and contract.
- [`README.md`](./README.md) — narrative overview and install instructions.
- [`docs/design.md`](./docs/design.md) — why round-level, outer-loop-only.
- [`docs/anti_patterns.md`](./docs/anti_patterns.md) — forbidden usage modes.

## Project-side contract (zero adapter)

Adopters contribute exactly two things:

1. An `evaluate(params: dict) -> dict | float` callable (any Optuna
   user already has this).
2. A config YAML/JSON with an `evaluate: "module:callable"` pointer
   that conforms to
   [`schemas/next_round_config.schema.json`](./schemas/next_round_config.schema.json).

All Optuna orchestration (sampler/pruner construction, `suggest_*`
dispatch, bundle export, `axis_coverage` enrichment, schema validation,
markdown rendering) lives in
[`scripts/round_runner.py`](./scripts/round_runner.py) and
[`scripts/round_adapter.py`](./scripts/round_adapter.py). A round is
one CLI call:

```bash
python scripts/round_runner.py run \
    --config <cfg> \
    --out-bundle <bundle.json> \
    --out-llm-input <llm_input.md>
```

Do **not** tell users to write an adapter module, export a bundle
dict by hand, or recompute `axis_coverage` — those are skill-owned.

## Codex-specific prompts

When Codex is acting as the outer-loop analyst between two Optuna rounds,
use the prompts under [`prompts/codex/`](./prompts/codex/):

- [`prompts/codex/analyze_round.md`](./prompts/codex/analyze_round.md) —
  analyse a finished round's bundle and produce a round report.
- [`prompts/codex/propose_next_round.md`](./prompts/codex/propose_next_round.md)
  — propose the next round's declarative JSON config.

## Hard constraints (apply to every Codex turn under this AGENTS.md)

1. The LLM is an **outer-loop analyst only**. No per-trial steering. No
   mid-round search-space changes. No LLM-as-objective. See
   [`docs/anti_patterns.md`](./docs/anti_patterns.md) for the full list.
2. Output is declarative JSON conforming to
   [`schemas/next_round_config.schema.json`](./schemas/next_round_config.schema.json)
   — no Python, no Optuna API calls.
3. Every `search_space` change MUST cite a specific bundle field in
   `provenance.rationale` and `provenance.diff_summary[*].evidence`.
4. All required `provenance` fields must be populated; use
   `provenance.kind = "llm_proposed"`. Operator-set top-level fields
   (`evaluate`, `direction`, `objective_name`, `study_id`) MUST be
   carried forward from the parent config unchanged unless the user
   explicitly asks for a change.
5. For "large changes" (drop a previously-important param, expand a range
   by >10×, switch sampler family, split the study), set
   `provenance.reviewer = { "kind": "human", "id": null, "approved_at": null }`
   — do not self-approve.

## Worked example

See [`examples/rag_example/`](./examples/rag_example/) — a round 01 → round
02 walkthrough with bundle, rendered LLM input, round report, and
next-round config. The [`examples/tabular_toy/`](./examples/tabular_toy/)
directory is an illustrative portability demonstration only and makes no
ML/DL empirical claim.

## If you (Codex) are asked to modify this repo itself

- Do not weaken the hard constraints above.
- Do not introduce per-trial LLM steering, LLM-as-objective, or any
  pattern listed in [`docs/anti_patterns.md`](./docs/anti_patterns.md).
- Preserve the validated-vs-illustrative distinction between `rag_example/`
  and `tabular_toy/`.
- Keep every change in [`examples/`](./examples/) consistent with the
  placeholder-hash policy described in [`README.md`](./README.md) and
  [`examples/rag_example/README.md`](./examples/rag_example/README.md).
