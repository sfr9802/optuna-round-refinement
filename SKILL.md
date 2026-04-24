---
name: refine
version: 0.1.0
description: >
  Round-level Optuna hyperparameter refinement with an LLM-in-the-outer-loop.
  Optuna samples trials; the LLM only analyses a finished round's bundle and
  proposes the next round's configuration. Model-agnostic, schema-validated,
  with explicit provenance.
invocation:
  triggers:
    - "한 라운드의 Optuna study가 끝나고 다음 라운드 설정을 결정해야 할 때"
    - "search space / sampler / fixed params를 라운드 단위로 진화시키려 할 때"
    - "study bundle을 기반으로 LLM이 '다음 라운드 제안'을 생성해야 할 때"
  non_triggers:
    - "단일 run 내에서 per-trial 피드백을 LLM에게 받으려 할 때"
    - "objective function 자체를 LLM이 실행/대체하려 할 때"
    - "search space를 라운드 중간에 바꾸려 할 때"
inputs:
  - path: schemas/study_bundle.schema.json
    role: input_contract
    description: finished round summary (stats, importances, top trials, …)
  - path: templates/llm_input.md
    role: rendering
    description: markdown rendering of the bundle for LLM consumption
outputs:
  - path: schemas/next_round_config.schema.json
    role: output_contract
    description: next-round Optuna configuration with provenance
  - path: templates/round_report.md
    role: rendering
    description: human-readable analyst report
dependencies:
  runtime:
    - optuna>=3.0
    - jsonschema>=4
  llm:
    - any chat model with tool/JSON output
---

# SKILL.md — optuna-round-refinement

## 0. Adapter contract at a glance

> **Use `build_study_bundle(...)` and `render_llm_input(...)` from
> [`scripts/round_adapter.py`](scripts/round_adapter.py).** Coverage
> enrichment (`statistics.axis_coverage`) and coverage notes are
> handled internally by the skill package. Downstream projects do
> **not** need to add custom adapters, compute coverage on the client
> side, or register a custom template/Handlebars helper. Upgrading the
> skill package is sufficient to get the safer boundary-handling
> behaviour; no new imports or function calls are required of an
> adapter beyond these two canonical entry points.

## 1. When to use this skill

Use it **between rounds** of an Optuna study, once the current round's trials
have all reached a terminal state (`COMPLETE`, `PRUNED`, or `FAIL`).

Typical triggers:

- A round of `N` trials finished; you want to decide the search space for
  round `N+1`.
- Importances or parallel-coordinate patterns suggest some params are now
  inert and should be frozen.
- Best trial value plateaued; you want to expand or shift a range.

## 2. When **not** to use this skill

Hard prohibitions (see [`docs/anti_patterns.md`](docs/anti_patterns.md)):

- **Per-trial steering.** The LLM must not see or influence in-flight trials.
- **Objective replacement.** The LLM must not implement or modify the
  objective function at runtime.
- **Mid-round changes.** The search space is frozen for the entire round.
  Changes take effect only in the next round.
- **Raw data exposure.** The LLM reads the study bundle, not training data,
  logs, or user PII.

## 3. Required inputs

The project-side adapter MUST provide, per round:

| Field | Source | Notes |
|-------|--------|-------|
| `study_bundle.json` | `build_study_bundle(raw, out_path=...)` | skill-owned; enriches axis_coverage + notes, validates, writes |
| `llm_input.md` | `render_llm_input(bundle, out_path=...)` | skill-owned; fills the template and bakes in the coverage-note column |
| `parent_config` | previous `next_round_config.json` (if any) | for provenance |

Neither `build_study_bundle` nor `render_llm_input` requires downstream
coverage logic or a custom template helper. Upgrading the skill package
gives the adapter the safer behaviour with no new imports or calls
beyond these two entry points.

The first round's input is the **initial config**, not a bundle — the LLM is
not invoked before round 1.

## 4. Expected outputs

The LLM MUST produce, for each transition R → R+1:

| Field | Schema / template |
|-------|-------------------|
| `round_report.md` | [`templates/round_report.md`](templates/round_report.md) |
| `next_round_config.json` | [`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json) |

`next_round_config.json` MUST include a `provenance` object (see §6).

## 5. Workflow steps

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Round R (Optuna, offline)                                           │
 │  1. load round_R_config.json                                         │
 │  2. run Optuna study with frozen search space                        │
 │  3. on completion: build_study_bundle(raw, out_path=…) writes the    │
 │     bundle with axis_coverage + coverage notes already baked in and  │
 │     schema-validated — no adapter-side coverage logic needed.        │
 └──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Round R → R+1 transition (LLM analyst, outer loop)                  │
 │  4. render bundle via scripts/round_adapter.py::render_llm_input     │
 │     (package-owned; fills templates/llm_input.md and resolves the    │
 │     coverage-note column — no downstream helper required).           │
 │  5. (LLM input is now on disk)                                       │
 │  6. run prompt: prompts/<tool>/analyze_round.md                      │
 │     → produces round_R_analysis.md                                   │
 │  7. run prompt: prompts/<tool>/propose_next_round.md                 │
 │     → produces round_R+1_config.json (draft)                         │
 │  8. validate draft against schemas/next_round_config.schema.json     │
 │  9. (optional) human reviewer approves / edits rationale             │
 │ 10. freeze round_R+1_config.json with hash + provenance              │
 └──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
                          Round R+1 …
```

## 6. Provenance fields

Every `next_round_config.json` MUST populate:

- `provenance.source_round_id` — id of the round whose bundle drove this config
- `provenance.source_bundle_hash` — sha256 of the canonicalised bundle JSON
- `provenance.parent_config_hash` — sha256 of the previous round's config
- `provenance.generated_at` — ISO-8601 UTC timestamp
- `provenance.generated_by` — `{ tool, model, prompt_version }`
- `provenance.reviewer` — `{ kind: "human"|"auto", id, approved_at }`
- `provenance.rationale` — free-text explanation of *why* each change was made

Configs missing any of these MUST be rejected by the adapter.

## 7. Separation: core vs. project adapter

This skill provides:

- schemas, prompts, templates, docs, examples,
- a small set of package-owned helpers in `scripts/round_adapter.py` that
  take responsibility for every safety-critical bundle enrichment step —
  **not** shims the adapter has to call on top of its own logic:
  - `build_study_bundle(raw, out_path=None, validate=True)` — canonical
    constructor (normalises axis_coverage, generates the per-param
    coverage note, schema-validates, optionally writes).
  - `load_study_bundle(path, validate=True)` — canonical reader that
    safe-normalises the loaded bundle (legacy bundles without
    axis_coverage stay "coverage unknown", as required by
    `docs/anti_patterns.md#a10`).
  - `write_study_bundle(bundle, out_path)` — normalise + validate + write.
  - `normalize_study_bundle(bundle)` — safe top-up used internally by
    the loaders; never stomps on trusted axis_coverage values from disk.
  - `render_llm_input(bundle, out_path=None)` — canonical markdown
    renderer that fills `templates/llm_input.md` AND resolves the
    coverage-note column inside the package. Projects do NOT need to
    register a `coverage_note` handlebars helper, extend the template
    context, or compute the note on the adapter side.

The project side provides:

- `export_study_bundle(study) → dict` — serialises an Optuna study to
  the bundle schema, then delegates to the skill's
  `build_study_bundle` for axis-coverage + coverage-note enrichment
  and schema validation.
- `apply_next_round_config(cfg) → optuna.Study` — builds an Optuna
  study from the config.
- `validate_and_hash(cfg_path) → config_hash` — jsonschema + sha256.

The project adapter is NOT expected to author coverage-related logic.
Upgrading this skill package gives the adapter the safer behaviour
automatically as long as bundles go through `build_study_bundle` /
`load_study_bundle` — no new import and no new call are needed beyond
what the adapter already wires up.

## 8. Forbidden patterns (enforcement)

Adapters SHOULD implement the following runtime checks:

1. Reject any `next_round_config.json` whose `provenance.source_bundle_hash`
   does not match a known bundle on disk.
2. Reject any config whose `search_space` was produced while a round was
   still running (check round's terminal state).
3. Reject LLM output that calls an Optuna API directly (the skill output is
   declarative config only).

See [`docs/anti_patterns.md`](docs/anti_patterns.md) for the full list.
