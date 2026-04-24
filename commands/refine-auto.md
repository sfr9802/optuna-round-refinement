---
description: Run N Optuna rounds unattended (auto-loop) via the optuna-round-refinement skill
argument-hint: '<config_path> <N>'
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

Arguments given to this command: `$ARGUMENTS`

This is the **explicit auto-loop form** of `/refine`. Use it when you
want to make clear — in the command itself — that you are running
multiple rounds unattended. Semantically it is equivalent to
`/refine <config_path> <N>` with `N >= 2`.

Parse `$ARGUMENTS` into exactly two tokens:

1. `<config_path>` — required. Path to the initial round config.
2. `<N>` — required positive integer. No default. Hard cap:
   `AUTO_LOOP_HARD_CAP = 50`.

If either token is missing, tell the user the expected form
(`/refine-auto <config_path> <N>`) and stop. Do NOT default N to
some value — the skill deliberately requires the operator to choose
the round count.

If the config file does not exist, stop and report.

If `N < 1`, stop and report (positive integer required). If `N > 50`,
explain the cap and ask whether to split the study or raise the cap
explicitly.

## Launch

```bash
OUT_DIR="run_output/auto_$(date +%Y%m%d_%H%M%S)"
PROPOSE_PROMPT="${CLAUDE_PLUGIN_ROOT}/prompts/claude_code/propose_next_round.md"
FINAL_PROMPT="${CLAUDE_PLUGIN_ROOT}/prompts/claude_code/final_summary.md"

python "${CLAUDE_PLUGIN_ROOT}/scripts/round_runner.py" auto \
    --config <config_path> \
    --rounds <N> \
    --llm-cmd "claude --print --append-system-prompt \"\$(cat '${PROPOSE_PROMPT}')\" < {llm_input} > {next_config}" \
    --llm-cmd-final "claude --print --append-system-prompt \"\$(cat '${FINAL_PROMPT}')\" < {trajectory} > {final_report}" \
    --out-dir "${OUT_DIR}"
```

Each round-to-round transition is produced by a fresh headless
`claude -p` subprocess. The outer session (this one) only
orchestrates; it does not do the per-round analysis itself. Token
cost is **linear in N**.

## When the loop finishes

Read these three files in order and present a concise summary:

1. `${OUT_DIR}/summary.md` — artifact index + global best.
2. `${OUT_DIR}/final_report.md` — final LLM review.
3. `${OUT_DIR}/trajectory.md` — only if the user asks for more detail.

Highlight: global best value + its source round, any anti-pattern
A10 ("narrow against UNSAMPLED EDGE") flagged in the final report,
and the recommendation (more rounds vs converged).

## If the loop fails partway

The runner preserves every completed round under
`${OUT_DIR}/round_NN/`. Read the last complete bundle and the error
message, diagnose, and propose a fix. Common causes: LLM emitted
invalid JSON, schema violation in a `search_space` change, evaluate
function raised.

Do NOT silently retry — surface the failure first.
