---
description: Run one Optuna HPO round (or N rounds, auto-mode) via the optuna-round-refinement skill
argument-hint: '<config_path> [N]'
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

Arguments given to this command: `$ARGUMENTS`

Parse `$ARGUMENTS` into two tokens:

1. `<config_path>` — required. Path to the round config (YAML or JSON)
   conforming to `schemas/next_round_config.schema.json`. Relative
   paths are resolved against the user's current working directory.
2. `[N]` — optional positive integer.
   - If `N` is absent OR `N == 1` → **single-round mode** (§A below).
   - If `N >= 2` → **auto-loop mode** for N rounds (§B below).

If `$ARGUMENTS` is empty, tell the user the expected form
(`/refine <config_path> [N]`) and walk SKILL.md §1 Step 1 to look for
an active config in their project (`experiment.active.yaml`,
`next_round_config.json`, etc.). Do NOT invent a config.

If the user supplied a path but the file does not exist, stop and
report — do not create one silently.

---

## §A. Single-round mode (`N == 1` or omitted)

Run exactly one Optuna round and let the current Claude Code session
drive the analysis + next-round proposal interactively. This matches
SKILL.md §1 Steps 3–8.

### Step 1 — Run the round

```bash
mkdir -p run_output
python "${CLAUDE_PLUGIN_ROOT}/scripts/round_runner.py" run \
    --config <config_path> \
    --out-bundle run_output/study_bundle.json \
    --out-llm-input run_output/llm_input.md
```

The CLI prepends the config file's parent directory to `sys.path` so
an `evaluate` callable living next to the YAML resolves automatically.
If the user's evaluate lives elsewhere, add
`--evaluate-search-path <dir>`.

### Step 2 — Analyze the round

Read `run_output/llm_input.md` and the bundle. Apply the analyst
prompt at `${CLAUDE_PLUGIN_ROOT}/prompts/claude_code/analyze_round.md`
to produce a round report matching
`${CLAUDE_PLUGIN_ROOT}/templates/round_report.md`. Save it as
`run_output/round_<NN>_analysis.md`.

### Step 3 — Propose the next round's config

Apply `${CLAUDE_PLUGIN_ROOT}/prompts/claude_code/propose_next_round.md`
to draft the next round's config. The LLM owns `search_space`,
sampler / pruner, `n_trials`, `rationale`, `diff_summary`. Mechanical
fields (`round_id` increment, provenance hashes, `evaluate` carryover)
can be left as sentinels — §1 of the skill documents which ones the
runner fills in when it re-enters the loop.

**Hard rules** (see `${CLAUDE_PLUGIN_ROOT}/docs/anti_patterns.md`):

- Never narrow against an UNSAMPLED EDGE (A10). Check the bundle's
  `statistics.axis_coverage.<p>.note` before every NARROW.
- Carry forward `evaluate`, `direction`, `objective_name`, `study_id`
  from the parent config unchanged.
- Every `search_space` change must cite a specific bundle field in
  `provenance.diff_summary[*].evidence`.

### Step 4 — Validate + freeze

Validate the draft against
`${CLAUDE_PLUGIN_ROOT}/schemas/next_round_config.schema.json` before
writing it to `run_output/round_<NN+1>_config.json`.

### Step 5 — Hand back to the user

Summarise: round number, best value, notable axis-coverage findings,
the diff vs parent config, whether a plateau stop is recommended. If
the user then says "run it", re-run from Step 1 with the new config.

---

## §B. Auto-loop mode (`N >= 2`)

Run N rounds back-to-back unattended. Each round-to-round transition
is produced by a **fresh headless `claude -p` subprocess**, so the
outer (this) Claude Code session is only the orchestrator — it does
not do the per-round analysis itself.

Hard cap: `AUTO_LOOP_HARD_CAP = 50`. If the user asked for more than
50, explain the cap exists to prevent runaway studies and ask
whether to split the study or raise the cap explicitly.

### Step 1 — Pick an output directory

Default to `run_output/auto_<YYYYMMDD_HHMMSS>/` unless the user has
specified one. Never overwrite an existing non-empty auto-loop dir
without asking.

### Step 2 — Launch the auto-loop

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

Notes on the invocation:

- `claude --print` runs a fresh, headless Claude Code instance per
  round. Nothing from this session leaks into those calls — they see
  only the stdin markdown (`llm_input.md` or `trajectory.md`) plus
  the appended system prompt.
- `{llm_input}`, `{next_config}`, `{trajectory}`, `{final_report}`
  are the skill's placeholder names; do **not** quote them yourself —
  the runner quotes substituted paths via `shlex.quote` (POSIX) or
  `"..."` (Windows) automatically.
- Token cost is **linear in N**: per-round calls see only that
  round's bundle, the final call sees a compact trajectory
  (per-round headline stats, importance drift, global best) rather
  than a concatenation of every bundle.

### Step 3 — Present the outcome

When the runner finishes, read these three files in order and
present a concise summary to the user:

1. `${OUT_DIR}/summary.md` — artifact index + global best across
   the study.
2. `${OUT_DIR}/final_report.md` — the final LLM review.
3. `${OUT_DIR}/trajectory.md` — only if the user asks for more
   detail.

Highlight: global best value + which round it came from, whether any
anti-patterns (A10 "narrow against UNSAMPLED EDGE") show up in the
final report, and whether the recommendation is "more rounds" or
"converged."

### Step 4 — If the run fails partway through

The runner preserves every completed round's artifacts under
`${OUT_DIR}/round_NN/`. Read `${OUT_DIR}/round_<last_complete>/bundle.json`
and the error message, diagnose (common causes: LLM emitted invalid
JSON, schema violation in a `search_space` change, evaluate function
raised), and propose a fix. Do NOT silently retry — report first.

---

## General rules (both modes)

- `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin's installed path at
  runtime. Do not hardcode absolute paths.
- Write outputs under a **gitignored** directory (`run_output/` is
  the convention) so sample artefacts committed to the user's repo
  are not overwritten.
- Do NOT attempt per-trial steering, objective replacement, or
  mid-round search-space changes. These are explicitly out of scope
  (see `${CLAUDE_PLUGIN_ROOT}/docs/anti_patterns.md`).
