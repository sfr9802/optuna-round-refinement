---
name: refine
version: 0.3.0
description: >
  Round-level Optuna hyperparameter refinement with an LLM-in-the-outer-loop.
  Optuna samples trials; the LLM only analyses a finished round's bundle and
  proposes the next round's configuration. Model-agnostic, schema-validated,
  with explicit provenance. The skill package owns all Optuna orchestration;
  the project side contributes only an evaluate(params)->dict callable.
invocation:
  triggers:
    - "Optuna 한 라운드를 실행하고 다음 라운드 설정까지 정하고 싶을 때"
    - "완료된 study bundle을 분석해 next_round_config.json을 만들어야 할 때"
    - "search space / sampler / fixed params를 라운드 단위로 진화시키려 할 때"
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
    - pyyaml
    - numpy
  llm:
    - any chat model with tool/JSON output
---

# SKILL.md — optuna-round-refinement

## 0. TL;DR for Claude when this skill is invoked

When the user invokes this skill in Claude Code, you are driving a
round-level HPO loop on their project. **Do not ask the user to write
an adapter.** The project has two artifacts only:

1. An `evaluate(params: dict) -> dict | float` callable somewhere in
   their code (any Optuna user already has this).
2. A config YAML / JSON conforming to
   [`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json)
   with an `evaluate: "module:callable"` pointer to (1).

Everything else — sampler / pruner construction, `suggest_*` dispatch,
bundle export, `axis_coverage` enrichment, schema validation, markdown
rendering — is owned by this skill package. You invoke one CLI and get
back a fully-enriched bundle + rendered LLM input.

Canonical skill-owned entry points (all in
[`scripts/round_adapter.py`](scripts/round_adapter.py)):

- `build_study_bundle(raw, out_path=…)` — normalise + validate + write
- `load_study_bundle(path)` — read + safe-normalise + validate
- `render_llm_input(bundle, out_path=…)` — fill
  `templates/llm_input.md` and bake the coverage-note column

And the orchestration CLI in
[`scripts/round_runner.py`](scripts/round_runner.py):

- `python scripts/round_runner.py run --config <cfg> --out-bundle <b> --out-llm-input <md>`
- `python scripts/round_runner.py render --bundle <b>`

## 1. Workflow when invoked from Claude Code

Follow these steps in order. Stop and ask the user only where explicitly
noted.

### Step 1 — Locate the active config

Look for an active round config in the user's project. Conventional
names, in order of preference:

- `experiment.active.yaml` / `experiment.active.json`
- `next_round_config.json` / `round_NN_config.json` (highest NN is the target)
- `optuna_round.yaml`

If none is found, tell the user what's needed (an initial config with
`evaluate:`, `search_space`, `sampler`, `pruner`, `n_trials`,
`provenance.kind = "initial"`) and offer to draft one next to their
`evaluate` function.

### Step 2 — Verify the `evaluate` pointer

Read the config and confirm `evaluate: "module:callable"` resolves.
If it does not, either help the user fix the pointer or scan their
project for a plausible evaluate-shaped function and propose the
correct spec. Do **not** invent an evaluate function silently — the
objective is the user's responsibility.

### Step 3 — Run the round

Invoke the skill-owned CLI via Bash (paths relative to the user's
project root):

```bash
python <SKILL_ROOT>/scripts/round_runner.py run \
    --config <path/to/config.yaml> \
    --out-bundle <path/to/run_output/study_bundle.json> \
    --out-llm-input <path/to/run_output/llm_input.md>
```

`<SKILL_ROOT>` is this repository's directory (resolve via plugin
install path or, when vendored, `third_party/optuna-round-refinement/`).
The CLI prepends the config file's parent directory to `sys.path` by
default, so an evaluate callable living next to the YAML resolves
without extra setup. Pass `--evaluate-search-path <dir>` when it lives
elsewhere.

Write run outputs under a **gitignored** path (`run_output/` is the
convention) so checked-in sample artefacts are not overwritten.

### Step 4 — Read the bundle and the rendered LLM input

Read the generated `study_bundle.json` and `llm_input.md`. The bundle
has `statistics.axis_coverage` (with per-param `note`) already baked
in — do not recompute coverage.

### Step 5 — Produce the round report

Use the analyst prompt at
[`prompts/claude_code/analyze_round.md`](prompts/claude_code/analyze_round.md)
with `llm_input.md` as the bundle context. Output conforms to
[`templates/round_report.md`](templates/round_report.md). Save it as
`round_<NN>_analysis.md` next to the bundle.

### Step 6 — Propose the next round's config

Use the proposal prompt at
[`prompts/claude_code/propose_next_round.md`](prompts/claude_code/propose_next_round.md).
Output is a JSON object conforming to
[`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json).

**Hard rules** (see [`docs/anti_patterns.md`](docs/anti_patterns.md)):

- Carry forward `evaluate`, `direction`, `objective_name`, `study_id`
  from the parent config unless the user explicitly asks you to change
  them — these are operator-set.
- Every `search_space` change must cite a specific bundle field in
  `provenance.rationale` and `provenance.diff_summary[*].evidence`.
- **Never narrow against an UNSAMPLED EDGE** (A10). If
  `axis_coverage.<p>.note` says `"upper edge UNSAMPLED"` or
  `"lower edge UNSAMPLED"`, treat the matching `boundary_hits` zero as
  *lack of evidence*, not negative evidence.
- If `axis_coverage` is absent (legacy bundle), treat coverage as
  unknown and prefer HOLD / EXPAND / random-sampler exploration over
  NARROW.

### Step 7 — Validate + freeze

Validate the draft with:

```bash
python -c "import json, jsonschema; \
    schema=json.load(open('schemas/next_round_config.schema.json')); \
    cfg=json.load(open('<draft>')); jsonschema.validate(cfg, schema)"
```

Fill provenance hashes: `source_bundle_hash = sha256(canonical_bundle_json)`,
`parent_config_hash = sha256(canonical_parent_config_json)`, both using
`json.dumps(..., sort_keys=True, separators=(",", ":"))` as the
canonical form (see §6). Write the frozen config as
`round_<NN+1>_config.json`.

### Step 8 — Hand back to the user

Summarise: round number, best value, notable axis-coverage findings,
the diff vs parent config, and next steps. If the user says "run it",
repeat from Step 3 with the new config.

## 2. Project-side contract (what the user writes)

| Artifact | Shape | Notes |
|----------|-------|-------|
| `evaluate.py` (or any module) | `def evaluate(params: dict) -> dict \| float` | Return `{"primary": <num>, "secondary": {...}}` or just a number. The runner merges `fixed_params` + sampled search-space values before calling. |
| config YAML/JSON | conforms to `schemas/next_round_config.schema.json` | Must include `evaluate: "mod:func"`. Operator-set fields (`evaluate`, `direction`, `objective_name`, `study_id`) typically carry forward unchanged across rounds. |

The user does **not** write:

- adapter code that builds a raw bundle dict,
- `suggest_*` dispatch,
- `axis_coverage` / `boundary_hits` computation,
- template-side handlebars helpers.

## 3. When **not** to use this skill

Hard prohibitions (see [`docs/anti_patterns.md`](docs/anti_patterns.md)):

- **Per-trial steering.** The LLM must not see or influence in-flight trials.
- **Objective replacement.** The LLM must not implement or modify the
  objective function at runtime. The user's `evaluate` is the objective.
- **Mid-round changes.** The search space is frozen for the entire
  round. Changes take effect only in the next round.
- **Raw data exposure.** The LLM reads the study bundle, not training
  data, logs, or user PII.

## 4. Expected outputs per round transition R → R+1

| Field | Schema / template |
|-------|-------------------|
| `round_<R>_analysis.md` | [`templates/round_report.md`](templates/round_report.md) |
| `round_<R+1>_config.json` | [`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json) |

Configs missing any required provenance field MUST be rejected.

## 5. Workflow diagram

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Round R                                                             │
 │                                                                      │
 │  scripts/round_runner.py run --config round_R_config.json            │
 │      ├── create_study(sampler, pruner)                               │
 │      ├── study.optimize( objective → evaluate(merged_params) )       │
 │      └── build_study_bundle(raw) → axis_coverage + coverage notes    │
 │          baked in, schema-validated; study_bundle.json written       │
 │          plus llm_input.md rendered by render_llm_input.             │
 └──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Round R → R+1 (LLM analyst in Claude Code / Codex)                  │
 │                                                                      │
 │  prompts/<tool>/analyze_round.md        → round_<R>_analysis.md      │
 │  prompts/<tool>/propose_next_round.md   → round_<R+1>_config.json    │
 │  jsonschema validate + fill sha256 provenance + freeze.              │
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
- `provenance.diff_summary[*].evidence` — for every search_space change,
  a bundle-field reference (e.g. `"axis_coverage.lr.note"`).

Canonical JSON for hashing: `json.dumps(obj, sort_keys=True,
separators=(",", ":"))`.

## 7. Low-level Python API (escape hatch)

The CLI covers the standard single-objective, in-memory flow. For
multi-objective studies, distributed storage, custom callbacks, or
non-standard driving loops, drop down to the library directly:

- `build_study_bundle(raw, out_path=None, validate=True)` — canonical
  constructor; normalises `axis_coverage`, generates the per-param
  coverage note, schema-validates, optionally writes.
- `load_study_bundle(path, validate=True)` — canonical reader;
  safe-normalises the loaded bundle (legacy bundles without
  `axis_coverage` stay "coverage unknown", per
  [`docs/anti_patterns.md#a10`](docs/anti_patterns.md)).
- `write_study_bundle(bundle, out_path)` — normalise + validate + write.
- `normalize_study_bundle(bundle)` — safe top-up used internally by the
  loaders; never stomps on trusted `axis_coverage` values from disk.
- `render_llm_input(bundle, out_path=None)` — canonical markdown
  renderer; resolves the coverage-note column inside the package. No
  downstream handlebars helper is required.

Custom drivers that route bundle writes through `build_study_bundle`
and reads through `load_study_bundle` get the same safer behaviour as
the CLI.

## 8. Forbidden patterns (enforcement)

Runner + validators SHOULD reject:

1. Any `next_round_config.json` whose `provenance.source_bundle_hash`
   does not match a bundle on disk.
2. Any config whose `search_space` was produced while a round was still
   running (check round's terminal state).
3. Any LLM output that calls an Optuna API directly — the skill output
   is declarative config only.

See [`docs/anti_patterns.md`](docs/anti_patterns.md) for the full list.
