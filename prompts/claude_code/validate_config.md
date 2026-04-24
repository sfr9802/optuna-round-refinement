# Prompt: validate_config (Claude Code)

> **prompt_version:** `0.1.0`

You are a **reviewer**, not a proposer. A draft `next_round_config.json` and
the StudyBundle that produced it are in context. Audit the draft. Do not
rewrite it — produce a verdict.

## Inputs

- `<draft_config.json>` — proposed next-round config.
- `<llm_input.md>` — rendering of the source StudyBundle.
- `<parent_config.json>` — config that produced the source round.

## Checks to run

### Schema & provenance
- [ ] `schema_version == "1.0"`
- [ ] `provenance.kind` is one of the allowed values
- [ ] all required `provenance.*` fields are populated (non-empty strings,
      real ISO-8601 timestamps, non-placeholder hashes unless
      `__FILL_AT_ADAPTER__`)
- [ ] `provenance.rationale` is non-empty and cites bundle fields by name

### Evidence
- [ ] every entry in `diff_summary` has a non-empty `evidence` string
- [ ] each `evidence` string names a field that actually exists in the
      bundle (`param_importances.<x>`, `statistics.boundary_hits.<x>.*`,
      `statistics.axis_coverage.<x>.*`, `top_trials[*]`, …)
- [ ] no `diff_summary` entry is an "unjustified change" (see
      `docs/anti_patterns.md` A8)
- [ ] **every `narrow` row's evidence cites `statistics.axis_coverage.<p>`**
      alongside `boundary_hits` / `top_trials`. A `narrow` justified
      solely by `boundary_hits.<p>.<side> == 0` against an UNSAMPLED
      EDGE is a hard fail (see `docs/anti_patterns.md` A10). If the
      source bundle lacks `statistics.axis_coverage` (legacy), any
      `narrow` whose evidence relies on `boundary_hits` alone is a hard
      fail.

### Anti-patterns (hard prohibitions)
- [ ] no Python, Optuna API calls, or executable code in the draft
- [ ] no fields outside the schema's `additionalProperties: false` envelope
- [ ] no raw data, PII, or training-example text in `rationale` / `notes`
- [ ] no suggestion of per-trial steering, LLM-as-objective, or mid-round
      changes

### Magnitude / risk
- Flag any of:
  - range expanded by >10× (log-scale too)
  - previously-important param dropped or frozen
  - sampler family changed
  - study split
  and ensure `provenance.reviewer.kind == "human"` for those.

### Stop-conditions
- [ ] `stop_conditions` non-trivial (at least one of `max_rounds`,
      `max_total_trials`, `target_value`, `no_improvement_rounds` set)

## Output format

Emit a single fenced JSON block:

\`\`\`json
{
  "verdict": "pass" | "pass_with_comments" | "reject",
  "findings": [
    { "severity": "info" | "warn" | "error", "check": "<check name>", "detail": "<one line>" }
  ],
  "suggested_edits": [
    "<plain-English suggestions, no JSON patches — let the proposer re-run>"
  ]
}
\`\`\`

Do not emit any text outside the fenced block.
