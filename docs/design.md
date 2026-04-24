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
| **Narrow** | `lr: [1e-5, 1e-1]` → `[1e-4, 1e-2]` | Best trials cluster in a sub-range AND both sides to be discarded were actually sampled (`statistics.axis_coverage.<p>` confirms). |
| **Shift** | `batch_size: [8, 64]` → `[32, 256]` | Best trials hit the upper boundary (sampled — `boundary_hits.<p>.high > 0` or `axis_coverage.<p>.sampled_max ≈ high`). |
| **Expand / Re-open** | `n_layers: [2, 4]` → `[2, 8]` | Best value at a sampled boundary and still improving; **or** a prior narrowing rationale is invalidated by a coverage gap / UNSAMPLED EDGE surfaced by `axis_coverage`. |
| **Hold** | `lr: [1e-4, 1e-2]` unchanged | One or more edges are UNSAMPLED and no other evidence supports a change — let the next round sample them. |
| **Exploration round** | Switch `sampler` to `RandomSampler` for one round | Multiple UNSAMPLED EDGEs across the search space, or post-narrow coverage collapse. Revert to TPE / CMA-ES in the round after. |
| **Freeze** | `dropout` removed, fixed at 0.1 | Low importance, high variance-cost. |
| **Split** | One study → two studies | Two clear clusters in parallel-coords. |
| **Add** | Introduce a new param | Ablation suggests a missing axis. |

Each action MUST be justified in `provenance.rationale` with a reference to
the relevant bundle field (`param_importances`, `statistics.boundary_hits`,
`statistics.axis_coverage`, `top_trials`, …). See
[`anti_patterns.md`](anti_patterns.md) for unjustified changes; in
particular, [`anti_patterns.md#a10`](anti_patterns.md) forbids narrowing
against an UNSAMPLED EDGE (a boundary where
`axis_coverage.<p>.sampled_<side>` never reached the configured edge).

### Coverage-driven re-open

`Expand` is not only "ran out of room at the edge" — it is also the
correct action when a previous round's **narrow** was justified by
`boundary_hits` alone, and a new round's `axis_coverage` now reveals that
the discarded side was never actually sampled. In that case the prior
narrowing rationale is invalidated and the range should be widened back to
(at least) its pre-narrow bounds, with the new evidence cited in
`provenance.rationale`.

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

The skill is deliberately thin. It ships a tiny set of canonical entry
points in `scripts/round_adapter.py` so that bundle safety-enrichment
(axis_coverage, coverage notes) happens **inside the package**, not on
the adapter side. The project adapter is expected to be small
(~100 LOC) and look roughly like:

```python
# project/opt_adapter.py
from pathlib import Path
import hashlib, json, jsonschema

from scripts.round_adapter import (
    build_study_bundle,   # construct + normalise + validate + (opt) write
    load_study_bundle,    # read-back + safe-normalise
    render_llm_input,     # canonical markdown renderer (coverage note baked in)
)

SCHEMA_CONFIG = json.loads(Path("schemas/next_round_config.schema.json").read_text())

def export_study_bundle(study, round_id: str, out_path: Path) -> dict:
    raw = {
        "schema_version": "1.0",
        "round_id": round_id,
        "study_id": study.study_name,
        # … populate per schema …
    }
    # build_study_bundle owns axis_coverage injection AND coverage-note
    # generation — the adapter does not call inject_axis_coverage by
    # hand or author a coverage_note helper on the template side.
    return build_study_bundle(raw, out_path=out_path)

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
the project repo. The skill stays generic AND owns every step that the
LLM analyst later depends on for boundary-hit disambiguation — there is
no coverage helper the adapter has to author.

## 6. Stop conditions

A round-based loop needs explicit stop conditions. The skill models three:

- **Budget**: `max_rounds`, `max_total_trials`.
- **Target**: `target_value` on the objective.
- **Plateau**: `no_improvement_rounds` with a `min_delta`.

Stop conditions are declared in `next_round_config.stop_conditions`. The
adapter checks them before kicking off round R+1.
