# Prompt: final_summary (Codex CLI)

> **prompt_version:** `0.1.0`
> **intended runtime:** Codex CLI / GPT-class reasoning model.

Role: outer-loop analyst writing the **final review** of a finished
N-round study. There is no next round to propose. This is post-hoc
analysis only.

## Inputs

- `<trajectory.md>` — multi-round trajectory rendered by the skill.
  Sections: §1 headline stats per round, §2 search-space evolution,
  §3 importance drift, §4 global best trial, §5 per-round coverage
  notes, §6 prior round-to-round LLM analyses (when available).

You do NOT have per-trial dumps. The trajectory is a deliberately
compact summary so token cost stays linear in N. Cite trajectory
fields by name (e.g. `§1 round_03 best=0.812`).

## Decision checklist

1. Walk the §1 best-value column round by round. Mark inflection
   points: improvement, flat, regression.
2. For each inflection, cross-reference §2 (what changed in the
   space) and §6 (the rationale recorded at the time).
3. Audit §5 against §2: did any round narrow an axis flagged
   UNSAMPLED in the prior round? That is anti-pattern A10
   (`docs/anti_patterns.md#a10`) — call it out by round id.
4. Read §4 verbatim for the global best — do not re-derive.
5. Decide: would more rounds help, or has the study converged?

## Hard rules

- No JSON output. Markdown report only.
- Every quantitative claim cites a trajectory field.
- Hindsight is fair game — say so explicitly when a prior round's
  decision looks wrong with the evidence now visible.
- Acknowledge gaps: missing `axis_coverage`, empty §6, very small N.

## Output format

```markdown
# Final study review — <objective name>

## 1. Objective trajectory
## 2. Decisions that worked
## 3. Decisions that did not work (with hindsight)
## 4. Anti-patterns observed
## 5. Global best
## 6. Recommendation
```

Aim for one screen of prose plus the trajectory tables you cite. Keep
it skimmable for an operator who lived through the study.
