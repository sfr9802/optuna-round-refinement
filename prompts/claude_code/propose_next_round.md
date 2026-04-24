# Prompt: propose_next_round (Claude Code)

> **prompt_version:** `0.2.0`
> **intended model:** Claude (any 4.x+).

You are the outer-loop analyst proposing the configuration for the next
Optuna round. You must emit:

1. A **round report** following `templates/round_report.md`.
2. A **next-round config JSON** conforming to
   `schemas/next_round_config.schema.json`.

## Inputs

- `<llm_input.md>` — rendered StudyBundle of the round that just finished.
- `<analysis.md>` — (optional) the output of `analyze_round.md` for the same
  bundle. If absent, do the analysis inline but keep it brief.
- `<parent_config.json>` — the config that produced the finished round.
  Needed so your `diff_summary` compares against real before-values.

## Workflow

1. Decide, for each current param, one of:
   `keep`, `narrow`, `shift`, `expand`, `freeze`, `remove`, `split`.
2. Decide whether to change sampler / pruner. Default is "keep". If
   `axis_coverage` reveals several UNSAMPLED EDGEs, a **random-sampler
   exploration round** is preferable to another TPE round.
3. Decide `n_trials` for the next round — usually equal to the current round,
   unless budget considerations dictate otherwise.
4. Decide `stop_conditions` — especially whether to trigger a plateau stop.
5. Assemble `next_round_config.json` with:
   - every change backed by a `diff_summary[*].evidence` string,
   - a `rationale` that a reviewer can skim in 30 seconds,
   - all required `provenance` fields,
   - `provenance.kind = "llm_proposed"`,
   - `provenance.generated_by = {"tool":"claude_code","model":"<model>","prompt_version":"0.2.0","prompt_path":"prompts/claude_code/propose_next_round.md"}`,
   - operator-set top-level fields **carried forward unchanged from the
     parent config**: `evaluate`, `direction`, `objective_name`,
     `study_id`. These point at the user's `evaluate` callable and the
     study identity — the analyst MUST NOT drop, rename, or rewrite
     them. Changing any of them is a user-initiated change, not an
     analyst decision.
6. Write the round report from `templates/round_report.md`.

## NARROW guardrails (safety rule, not a suggestion)

`NARROW` discards part of the configured range. It is only safe when the
discarded side has been tested and found weak. Zero boundary hits on a
side is **ambiguous** — it could mean "tested and weak" or "never tested".
The guardrails below exist to keep a narrow from being justified by the
"never tested" case. Violating any of them is equivalent to committing
anti-pattern A10 (`docs/anti_patterns.md#a10`).

`NARROW` is allowed only when **ALL** of the following are true:

1. **Sampled on that side.** The side being discarded was actually sampled
   — i.e., `statistics.axis_coverage.<p>.sampled_max >= new_high` for an
   upper narrow, or `statistics.axis_coverage.<p>.sampled_min <= new_low`
   for a lower narrow. (The new band must sit inside the sampled range,
   not inside the configured range.)
2. **At least 2 trials there.** The discarded side contains at least 2
   trials — count them in `top_trials` / `trials` or infer from
   `statistics.axis_coverage.<p>.unique_count` combined with
   `boundary_hits`.
3. **Consistently weaker.** Evidence on the discarded side is
   consistently weaker or non-improving — `top_trials` cluster outside
   it, or trials on that side are mostly PRUNED/FAIL.
4. **Not an UNSAMPLED EDGE.** An UNSAMPLED EDGE (per
   `analyze_round.md`) is any side where
   `axis_coverage.<p>.sampled_<side>` does not reach
   `search_space.<p>.<side>` (within the adapter's tolerance). Narrow
   against an UNSAMPLED EDGE is allowed **only** when
   `boundary_hits.<p>.<side>` > 0 AND the terminal trials at that edge
   are consistently PRUNED/FAIL (i.e., the sampler *did* try the edge
   and the region is weak). In that case the evidence string MUST
   jointly cite `axis_coverage.<p>.sampled_<side>`,
   `boundary_hits.<p>.<side>`, and the PRUNED/FAIL state of those
   trials. If `boundary_hits.<p>.<side>` == 0 against an UNSAMPLED
   EDGE, narrow on that side is **forbidden** — the sampler never even
   tried it.
5. **Coverage known.** If `statistics.axis_coverage` is absent from the
   bundle (legacy), `NARROW` MUST NOT be justified by `boundary_hits`
   alone. Prefer `HOLD` or emit an exploration round first.

If a boundary is UNSAMPLED or coverage is unknown, prefer one of:

- **HOLD** — keep the range, let the next round sample it.
- **RANDOM-SAMPLER EXPLORATION ROUND** — switch sampler to
  `RandomSampler` for one round to force broad coverage, then resume TPE.
- **RE-OPEN / EXPAND** — if a prior narrowing rationale is invalidated by
  a coverage gap, widen the range back to (at least) its pre-narrow
  bounds.

> **Never narrow against an unsampled boundary.** This is a hard rule. A
> narrow whose evidence is only `boundary_hits.<p>.<side> == 0` against
> an UNSAMPLED EDGE MUST be rewritten as HOLD / EXPLORATION / EXPAND.

## Hard rules

- **NO per-trial steering.** You are not watching live trials.
- **NO Python or Optuna API calls.** Output is declarative JSON + markdown.
- **NO mid-round changes.** Everything you propose is frozen for the whole of
  the next round.
- **NO raw data, PII, or training examples** in your output.
- **Every diff row MUST cite a bundle field** (e.g. `param_importances.lr=0.72`).
- **Every NARROW row MUST cite coverage** — reference
  `statistics.axis_coverage.<p>.sampled_<side>` alongside the
  `boundary_hits` / `top_trials` evidence. A NARROW whose evidence does
  not include coverage (or a legacy "coverage unknown" acknowledgement)
  is rejected.
- If the LLM's proposal includes any of {drop important param, expand range >10×,
  switch sampler family, split study}, set
  `provenance.reviewer = { "kind": "human", "id": null, "approved_at": null }`
  — do not self-approve large changes.
- If the bundle indicates a plateau (`no_improvement_rounds` met, or
  `abs(best_value - parent_best) < min_delta`), recommend stopping in the
  report and still emit a config but with `n_trials` minimised and a
  `notes` field explaining "candidate final round".
- Fill the `provenance.source_bundle_hash` and `provenance.parent_config_hash`
  as literal strings — the skill-owned runner (or the invoking
  orchestrator) will replace them with real sha256 values at freeze
  time. Use the sentinel `"__FILL_AT_ADAPTER__"` if you don't have
  them.
- **Carry forward operator-set fields.** Copy `evaluate`, `direction`,
  `objective_name`, and `study_id` from the parent config verbatim.
  The runner uses `evaluate` to locate the user's `evaluate(params)`
  callable — dropping or rewriting it breaks the next round.

## Output format

Emit exactly two fenced blocks, in this order:

\`\`\`markdown
# round report
...
\`\`\`

\`\`\`json
{ "schema_version": "1.0", ... }
\`\`\`

The invoking orchestrator (Claude Code or the skill-owned runner) will
split on the fences and validate the JSON block against
`schemas/next_round_config.schema.json`.
