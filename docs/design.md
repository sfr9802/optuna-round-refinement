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
  human, or a rule-based script without changing the user's
  `evaluate` callable or the skill-owned runner.

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
bundle hash that drove its changes. The skill-owned runner recomputes
these hashes at load time and refuses to run if they don't match — this
prevents silent drift when a bundle is re-exported or hand-edited.

## 5. Project-side contract (zero adapter)

The skill is deliberately thin. It ships:

- `scripts/round_runner.py` — CLI + Python orchestration that owns
  sampler/pruner construction, `trial.suggest_*` dispatch, bundle export,
  and delegation to the canonical bundle entry points below.
- `scripts/round_adapter.py` — pure-Python bundle helpers
  (`build_study_bundle`, `load_study_bundle`, `render_llm_input`, …)
  that own axis_coverage enrichment and coverage-note generation.

The project side provides **exactly one callable** and one config YAML:

```python
# project/evaluate.py
from typing import Any, Dict

def evaluate(params: Dict[str, Any]) -> Dict[str, Any]:
    # train / score one trial using the merged params dict
    # (sampled search-space values + fixed_params).
    return {"primary": val_auc, "secondary": {"train_time_s": 12.3}}
```

```yaml
# project/experiment.active.yaml
evaluate: "evaluate:evaluate"
direction: "maximize"
objective_name: "val_auc"
round_id: "round_01"
n_trials: 50
sampler: { type: "TPESampler", params: {}, seed: 42 }
pruner:  { type: "MedianPruner", params: {} }
search_space: { ... }
provenance: { kind: "initial", generated_at: "...", generated_by: { tool: "human" }, rationale: "..." }
```

A round is then one CLI call:

```bash
python <skill>/scripts/round_runner.py run \
    --config experiment.active.yaml \
    --out-bundle run_output/study_bundle.json \
    --out-llm-input run_output/llm_input.md
```

Everything project-specific (dataset loading, objective, logging) stays
inside `evaluate` and whatever modules it imports. The skill stays
generic AND owns every step the LLM analyst later depends on for
boundary-hit disambiguation — there is no adapter, no coverage helper,
and no bundle-construction code on the project side.

### Low-level escape hatch

Callers that need multi-objective studies, distributed storage, or
custom Optuna callbacks can skip the CLI and drive their own loop,
routing bundle writes through `build_study_bundle(raw, out_path=…)` and
reads through `load_study_bundle(path)`. Either path gets the same
safer boundary-handling behaviour.

## 6. Stop conditions

A round-based loop needs explicit stop conditions. The skill models three:

- **Budget**: `max_rounds`, `max_total_trials`.
- **Target**: `target_value` on the objective.
- **Plateau**: `no_improvement_rounds` with a `min_delta`.

Stop conditions are declared in `next_round_config.stop_conditions`. The
runner's caller (or the skill's orchestrator in Claude Code) checks
them before kicking off round R+1.
