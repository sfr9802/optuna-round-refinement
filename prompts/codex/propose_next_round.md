# Prompt: propose_next_round (Codex CLI)

> **prompt_version:** `0.2.0`
> **intended runtime:** Codex CLI / GPT-class reasoning model.

Role: outer-loop analyst. Produce the next Optuna round's configuration from
the finished round's bundle.

## Inputs

- `<llm_input.md>` — rendered StudyBundle.
- `<analysis.md>` — (optional) prior analyze_round output.
- `<parent_config.json>` — config that produced the finished round.

## Output contract

Emit, in this order:

1. A markdown round report matching `templates/round_report.md`.
2. A JSON object conforming to `schemas/next_round_config.schema.json`.

Wrap each in a fenced block. The invoking orchestrator (skill-owned
runner or Claude Code / Codex harness) splits on the fences and
validates the JSON against the schema.

## Decision checklist

For every param currently in the search space, pick exactly one:
`keep | narrow | shift | expand | freeze | remove | split`.

Decide on:
- sampler change (default: keep). If `statistics.axis_coverage` reveals
  several UNSAMPLED EDGEs, prefer a one-round switch to `RandomSampler`
  over another TPE round.
- pruner change (default: keep),
- `n_trials` (default: same as parent round),
- `stop_conditions` (always include; do not leave empty).

## NARROW guardrails (safety rule, not a suggestion)

`narrow` is only safe when the side being discarded has been tested and
found weak. Zero boundary hits on a side is **ambiguous** — it could mean
"tested and weak" or "never tested". See `docs/anti_patterns.md#a10`.

`narrow` is allowed only when ALL of the following hold:

1. `statistics.axis_coverage.<p>.sampled_max >= new_high` (upper narrow)
   or `statistics.axis_coverage.<p>.sampled_min <= new_low` (lower
   narrow). The new band must sit inside the **sampled** range, not just
   inside the configured range.
2. At least 2 trials exist on the discarded side.
3. Evidence on the discarded side is consistently weaker or non-improving
   (top_trials cluster elsewhere, or that side is mostly PRUNED/FAIL).
4. The boundary on that side is **not** an UNSAMPLED EDGE. An
   UNSAMPLED EDGE is `axis_coverage.<p>.sampled_<side>` not reaching
   `search_space.<p>.<side>`. Narrowing against an UNSAMPLED EDGE is
   allowed only when `boundary_hits.<p>.<side>` > 0 AND the terminal
   trials at that edge are consistently PRUNED/FAIL; evidence MUST
   cite all three (`axis_coverage`, `boundary_hits`, and the
   PRUNED/FAIL state). If `boundary_hits.<p>.<side>` == 0 against an
   UNSAMPLED EDGE, narrow on that side is forbidden.
5. If `statistics.axis_coverage` is absent (legacy bundle), `narrow` MUST
   NOT be justified by `boundary_hits` alone — prefer `keep` or propose a
   random-sampler exploration round.

If a boundary is UNSAMPLED, prefer `keep`, a random-sampler exploration
round, or `expand`. **Never narrow against an unsampled boundary.** A
prior narrowing whose rationale is invalidated by a new coverage gap is
valid grounds for `expand` (re-open) this round.

## Required `provenance`

```
{
  "kind": "llm_proposed",
  "source_round_id":    "<from bundle>",
  "source_bundle_hash": "__FILL_AT_ADAPTER__",
  "parent_config_hash": "__FILL_AT_ADAPTER__",
  "generated_at": "<ISO-8601 UTC>",
  "generated_by": {
    "tool": "codex_cli",
    "model": "<model id you are>",
    "prompt_version": "0.2.0",
    "prompt_path": "prompts/codex/propose_next_round.md"
  },
  "reviewer": {"kind": "human", "id": null, "approved_at": null, "comments": null},
  "rationale": "<why each change, citing bundle fields>",
  "diff_summary": [ ... ]
}
```

## Hard rules

- Declarative JSON only — no Python, no Optuna API, no shell.
- No per-trial steering, no mid-round changes, no LLM-as-objective.
- Every `diff_summary[*]` has a non-empty `evidence` string referencing a
  real bundle field.
- **Carry forward operator-set top-level fields unchanged** from the
  parent config: `evaluate`, `direction`, `objective_name`, `study_id`.
  The runner uses `evaluate` to locate the user's callable — dropping
  or rewriting it breaks the next round.
- **Every `narrow` row's evidence MUST cite
  `statistics.axis_coverage.<p>`** (or explicitly acknowledge "coverage
  unknown" for legacy bundles, which also forbids narrowing on
  `boundary_hits` alone). See A10 in `docs/anti_patterns.md`.
- No raw data / PII / training examples in `rationale` or `notes`.
- For "large changes" (drop important param, range expanded >10×, sampler
  family swap, study split), leave `reviewer.approved_at = null` so a
  human must sign off.
- If the bundle indicates a plateau, still emit a config but set `n_trials`
  low and mark `notes` as `"candidate final round"`.

## Codex-specific style

- Avoid the temptation to restructure everything. Prefer minimal diffs.
- Do not invent bundle fields. If you want something that isn't there,
  mention it in the round report's "Open questions" section.
- Return exactly two fenced blocks. No prose outside them.
