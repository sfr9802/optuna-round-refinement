# Design notes

## 1. Why round-level, not per-trial?

Optuna already performs Bayesian / TPE / CMA-ES sampling at the trial level.
These samplers have:

- well-understood convergence properties,
- cheap, deterministic decision cost (μs per trial),
- strong empirical baselines.

An LLM inserted into the per-trial loop would:

- add seconds-to-minutes of latency per trial,
- introduce non-determinism into the sampler,
- create a feedback loop where the LLM's priors can reinforce bad regions
  before enough evidence is collected,
- make reproducibility from a seed impossible.

The value an LLM *can* add is **structural reasoning over a completed round**:
"importance of `lr` is near zero; freeze it at its median"; "pruning rate in
the low-`dropout` region is 80%, shrink that range"; "two clear clusters in
the parallel-coordinates plot, split into two sub-studies".

These are decisions that normally require a human researcher to sit down after
a run and squint at plots. That is the niche this skill automates.

## 2. Outer loop vs inner loop

```
inner loop  =  Optuna   (seconds × trials)
outer loop  =  Analyst  (one LLM call per round)
```

The skill enforces this split at the schema level: the LLM only ever produces
a *declarative* `next_round_config.json`. It cannot call Optuna APIs, cannot
emit Python, cannot attach to a running study.

This split gives:

- **determinism**: re-running the inner loop with the same config + seed
  reproduces the round.
- **auditability**: every config change is a diff between two JSON files
  with full provenance.
- **model-agnosticism**: the LLM can be swapped for a smaller model, a
  human, or a rule-based script without changing the adapter.

## 3. Search-space evolution strategy

The LLM may propose any of the following between rounds:

| Action | Example | When appropriate |
|--------|---------|------------------|
| **Narrow** | `lr: [1e-5, 1e-1]` → `[1e-4, 1e-2]` | Best trials cluster in a sub-range. |
| **Shift** | `batch_size: [8, 64]` → `[32, 256]` | Best trials hit the upper boundary. |
| **Expand** | `n_layers: [2, 4]` → `[2, 8]` | Best value at boundary and still improving. |
| **Freeze** | `dropout` removed, fixed at 0.1 | Low importance, high variance-cost. |
| **Split** | One study → two studies | Two clear clusters in parallel-coords. |
| **Add** | Introduce a new param | Ablation suggests a missing axis. |

Each action MUST be justified in `provenance.rationale` with a reference to
the relevant bundle field (`param_importances`, `statistics`, `top_trials`,
etc.). See [`anti_patterns.md`](anti_patterns.md) for unjustified changes.

## 4. Provenance design

Provenance is a **chain**, not a single field:

```
round_01_config ──► round_01_bundle ──► round_02_config ──► round_02_bundle ──► …
      ▲                  │                     ▲                   │
      └── hash ──────────┘                     └── hash ───────────┘
```

Every `next_round_config.json` stores both its parent config hash and the
bundle hash that drove its changes. The adapter recomputes these hashes at
load time and refuses to run if they don't match — this prevents silent
drift when a bundle is re-exported or hand-edited.

## 5. Adapter pattern

The skill is deliberately thin. It ships **no** Python. The project-side
adapter is expected to be small (~100 LOC) and look roughly like:

```python
# project/opt_adapter.py
from pathlib import Path
import hashlib, json, jsonschema

SCHEMA_BUNDLE = json.loads(Path("schemas/study_bundle.schema.json").read_text())
SCHEMA_CONFIG = json.loads(Path("schemas/next_round_config.schema.json").read_text())

def export_study_bundle(study, round_id: str, out_path: Path) -> dict:
    bundle = {
        "schema_version": "1.0",
        "round_id": round_id,
        "study_id": study.study_name,
        # … populate per schema …
    }
    jsonschema.validate(bundle, SCHEMA_BUNDLE)
    out_path.write_text(json.dumps(bundle, indent=2, sort_keys=True))
    return bundle

def validate_and_hash(cfg_path: Path) -> str:
    cfg = json.loads(cfg_path.read_text())
    jsonschema.validate(cfg, SCHEMA_CONFIG)
    canon = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canon).hexdigest()

def apply_next_round_config(cfg: dict) -> "optuna.Study":
    # translate search_space + sampler + pruner → optuna.Study
    ...
```

Everything project-specific (dataset loading, objective, logging) stays in
the project repo. The skill stays generic.

## 6. Stop conditions

A round-based loop needs explicit stop conditions. The skill models three:

- **Budget**: `max_rounds`, `max_total_trials`.
- **Target**: `target_value` on the objective.
- **Plateau**: `no_improvement_rounds` with a `min_delta`.

Stop conditions are declared in `next_round_config.stop_conditions`. The
adapter checks them before kicking off round R+1.
