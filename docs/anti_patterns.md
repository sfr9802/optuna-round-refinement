# Anti-patterns — things this skill explicitly forbids

Violating any of the rules below voids the skill's guarantees (reproducibility,
auditability, model-agnosticism). Adapters SHOULD enforce these at runtime.

---

## A1. Per-trial LLM steering  *(hard prohibition)*

**Bad:**
```python
for trial in study:
    params = trial.suggest_params()
    hint = llm.ask(f"Is {params} promising? Shall I prune?")
    if hint == "prune":
        raise TrialPruned
```

**Why it's forbidden:**

- Breaks Optuna's sampler assumptions (the sampler must be the sole oracle
  over trial history).
- Introduces nondeterminism that seeds can't reproduce.
- Lets the LLM's priors overwhelm the actual sampling evidence.
- Cost per trial explodes (LLM latency ≫ sampler latency).

**Instead:** let Optuna finish the round; have the LLM analyse the bundle
and freeze / narrow / shift the range *in the next round's config*.

---

## A2. LLM-as-objective  *(hard prohibition)*

**Bad:** using an LLM to produce the trial's objective value, or to
replace parts of the training loop.

**Why it's forbidden:** the objective must be deterministic given the trial
params and a seed. LLM-scored objectives are noisy, expensive, and
un-auditable.

**Instead:** keep the objective pure. If you want LLM-based evaluation
(e.g. for a RAG quality score), wrap it as a *fixed, versioned, cached*
function that the objective calls — and treat the LLM version as a
hyperparameter of the study, not a free variable.

---

## A3. Mid-round search-space mutation  *(hard prohibition)*

**Bad:** updating `study.sampler` or adding `trial.suggest_*` branches
while the round is still running.

**Why it's forbidden:** destroys the round's statistical interpretation;
importances, Pareto fronts, and any TPE model built mid-run become
meaningless.

**Instead:** the search space is frozen for the entire round. All changes
take effect only in the next round's config.

---

## A4. Raw-data exposure to the LLM  *(hard prohibition)*

**Bad:** pasting training examples, model outputs, user prompts, or log
lines into the LLM round analyst.

**Why it's forbidden:**

- Privacy / PII leakage.
- Token waste — the LLM's job is structural reasoning over *summaries*.
- Trains the LLM to pattern-match on dataset content rather than on
  sampler statistics.

**Instead:** the LLM sees only the fields defined by
`schemas/study_bundle.schema.json`. If a statistic isn't in the schema,
add it to the schema — don't smuggle it in as raw text.

---

## A5. Unversioned configs  *(hard prohibition)*

**Bad:** editing `next_round_config.json` by hand without updating
`provenance` or recomputing hashes.

**Why it's forbidden:** breaks the provenance chain; the round is no
longer reproducible.

**Instead:** any edit — manual or LLM-generated — MUST produce a new
`generated_at`, a fresh `rationale`, and updated `source_bundle_hash` /
`parent_config_hash`.

---

## A6. LLM-generated Python / Optuna calls  *(hard prohibition)*

**Bad:** prompting the LLM to "write the Optuna code for the next round".

**Why it's forbidden:** mixes configuration with executable code,
bypassing schema validation and hash verification; invites arbitrary
code execution.

**Instead:** the LLM's output is declarative JSON that conforms to
`schemas/next_round_config.schema.json`. The adapter translates it into
Optuna calls.

---

## A7. No human review on large changes  *(soft warning)*

If the LLM proposes any of:

- dropping a previously-important parameter,
- expanding a range by >10×,
- switching the sampler family (TPE → CMA-ES etc.),
- splitting the study,

`provenance.reviewer.kind` SHOULD be `"human"`, not `"auto"`. Adapters
MAY refuse to run such configs without human approval.

---

## A8. Unjustified changes  *(soft warning)*

Every `search_space` diff must map to a specific bundle field
(`param_importances`, `statistics.boundary_hits`,
`statistics.axis_coverage`, `top_trials`, …) cited in
`provenance.rationale`. A change without a citation is a smell —
either the LLM hallucinated or the bundle is missing a field it should
have. `narrow` actions specifically MUST cite `axis_coverage` (or state
"coverage unknown" for legacy bundles); see A10.

---

## A9. Skipping round 1 validation  *(soft warning)*

The first round has no bundle, so `provenance.source_bundle_hash` is
absent. Adapters SHOULD still validate the initial config against
`next_round_config.schema.json` with a `provenance.kind: "initial"`
marker and human reviewer.

---

## A10. Narrowing against an unsampled boundary  *(hard prohibition)*

**Bad:** narrowing a param's range because `statistics.boundary_hits.<p>.high = 0`,
without first checking whether the upper edge was actually *sampled*.

**Why it's forbidden:**

- `statistics.boundary_hits.<p>.high = 0` is AMBIGUOUS. It collapses two
  very different cases:
  - **A) sampled-but-poor** — the upper edge was sampled and performed
    poorly, so no trial "clustered" at the edge. This IS evidence the
    edge is unhelpful and narrowing is safe.
  - **B) unsampled edge** — the sampler (TPE / Random / etc.) simply
    never explored the upper edge. `boundary_hits = 0` is the expected
    value for unsampled regions. This is **lack of evidence**, not
    evidence that the edge is bad.
- Reading (B) as if it were (A) makes the search space silently
  collapse around the initial random-startup region, which is almost
  always worse than the configured range after only a few rounds.
- Once the range has narrowed, the sampler cannot recover the discarded
  region without an explicit EXPAND action — so the mistake
  compounds across rounds.

**Safer behaviour (enforced by the skill):**

- The bundle carries `statistics.axis_coverage.<p>` with
  `sampled_min` / `sampled_max` / `unique_count` AND a human-readable
  `note` — all populated automatically by the skill's canonical entry points (`scripts/round_adapter.py::build_study_bundle`,
  `load_study_bundle`, `normalize_study_bundle`). Downstream adapters
  do NOT compute coverage or the coverage note by hand; upgrading the
  skill package is sufficient to get the safer behaviour.
- The analyst MUST compare `sampled_min|max` against `search_space.<p>.low|high`
  before citing any boundary hit. If `sampled_max < configured_high`,
  the upper side is labelled **UNSAMPLED EDGE**, and the same for the
  lower side with `sampled_min > configured_low`. The pre-baked
  `statistics.axis_coverage.<p>.note` already encodes this
  classification for the rendered bundle.
- An UNSAMPLED EDGE with `boundary_hits.<p>.<side> == 0` (sampler never
  even tried that edge) MUST NOT be cited as justification for
  narrowing. Narrowing against a never-tried edge is the failure this
  anti-pattern forbids.
- An UNSAMPLED EDGE with `boundary_hits.<p>.<side> > 0` where those
  terminal trials are consistently PRUNED/FAIL is "attempted but
  weak" — narrowing is allowed, but the evidence string MUST jointly
  cite `axis_coverage.<p>.sampled_<side>`, `boundary_hits.<p>.<side>`,
  and the PRUNED/FAIL state of those trials. Citing
  `boundary_hits` alone is insufficient.
- When narrowing is forbidden by the above, the proposer picks:
  - `HOLD` (keep the range),
  - a **RANDOM-SAMPLER EXPLORATION ROUND** (one round of
    `RandomSampler` to force coverage), or
  - `EXPAND` / RE-OPEN (if a prior narrow is invalidated by the
    newly-visible coverage gap).
- If `statistics.axis_coverage` is absent (legacy bundle), coverage is
  treated as **unknown** and `NARROW` MUST NOT be justified by
  `boundary_hits` alone. Prefer `HOLD` or an exploration round.

See `schemas/study_bundle.schema.json` (`statistics.axis_coverage`),
`templates/llm_input.md` §4, and
`prompts/claude_code/propose_next_round.md` "NARROW guardrails".

---

## Summary table

| # | Rule | Severity |
|---|------|----------|
| A1 | No per-trial LLM steering | hard |
| A2 | No LLM-as-objective | hard |
| A3 | No mid-round search-space changes | hard |
| A4 | No raw-data exposure | hard |
| A5 | No unversioned configs | hard |
| A6 | No LLM-generated Optuna code | hard |
| A7 | Large changes require human review | soft |
| A8 | All changes must cite bundle evidence | soft |
| A9 | Round 1 still needs schema validation | soft |
| A10 | Never narrow against an unsampled boundary | hard |
