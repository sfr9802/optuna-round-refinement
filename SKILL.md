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
| `study_bundle.json` | `export_study_bundle(study)` | conforms to schema |
| `llm_input.md` | render template | bundle → markdown |
| `parent_config` | previous `next_round_config.json` (if any) | for provenance |

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
 │  3. on completion: export_study_bundle() → round_R_bundle.json       │
 │  4. validate bundle against schemas/study_bundle.schema.json         │
 └──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Round R → R+1 transition (LLM analyst, outer loop)                  │
 │  5. render bundle with templates/llm_input.md                        │
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

This skill provides only:

- schemas, prompts, templates, docs, examples

The project side provides:

- `export_study_bundle(study) → dict` — serialises an Optuna study to the bundle schema
- `apply_next_round_config(cfg) → optuna.Study` — builds an Optuna study from the config
- `render_llm_input(bundle, out_path)` — fills `templates/llm_input.md`
- `validate_and_hash(cfg_path) → config_hash` — jsonschema + sha256

No adapter code lives in this repo.

## 8. Forbidden patterns (enforcement)

Adapters SHOULD implement the following runtime checks:

1. Reject any `next_round_config.json` whose `provenance.source_bundle_hash`
   does not match a known bundle on disk.
2. Reject any config whose `search_space` was produced while a round was
   still running (check round's terminal state).
3. Reject LLM output that calls an Optuna API directly (the skill output is
   declarative config only).

See [`docs/anti_patterns.md`](docs/anti_patterns.md) for the full list.
