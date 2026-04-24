# Auto-loop demo

End-to-end demo of the `auto` subcommand: N rounds back-to-back with an
LLM-driven transition between each pair of rounds and a final
study-wide review after the last round.

The user MUST choose the round count when invoking the skill — there
is no default. The hard cap is `AUTO_LOOP_HARD_CAP = 50` rounds.

## Files

- `initial_config.yaml` — round 1 config. `evaluate: "evaluate:evaluate"`
  resolves to `examples/tabular_toy/evaluate.py`, so this demo reuses
  the toy MLP HPO from the other example.
- `stub_llm.py` — offline stub for `--llm-cmd` and `--llm-cmd-final`.
  Emits schema-valid placeholder outputs so the loop runs without a
  real LLM call. Replace with a real invocation for production.

## Run with the stub (no LLM API needed)

From the repo root:

```bash
python scripts/round_runner.py auto \
  --config examples/auto_loop/initial_config.yaml \
  --rounds 3 \
  --llm-cmd 'python examples/auto_loop/stub_llm.py per-round --bundle {bundle} --out {next_config}' \
  --llm-cmd-final 'python examples/auto_loop/stub_llm.py final --in {trajectory} --out {final_report}' \
  --out-dir examples/auto_loop/run_output \
  --evaluate-search-path examples/tabular_toy
```

After it finishes:

```
examples/auto_loop/run_output/
├── round_01/
│   ├── config.json          ← canonicalised input config (hashed for provenance)
│   ├── bundle.json          ← Optuna study output
│   ├── llm_input.md         ← rendered for the LLM
│   ├── analysis.md          ← (empty for stub; real LLM writes round_report here)
│   └── next_config.json     ← LLM-produced + skill-normalised next round config
├── round_02/...
├── round_03/                ← last round: only config / bundle / llm_input
│   ├── config.json
│   ├── bundle.json
│   └── llm_input.md
├── trajectory.md            ← input to --llm-cmd-final
├── final_report.md          ← LLM-written study-wide review
└── summary.md               ← round-by-round artifact index + global best
```

## Run with a real LLM

Swap the stub for any CLI that reads markdown on stdin and writes JSON
on stdout. Two common patterns:

### Claude Code (headless)

```bash
python scripts/round_runner.py auto \
  --config examples/auto_loop/initial_config.yaml \
  --rounds 5 \
  --llm-cmd 'claude -p "$(cat prompts/claude_code/propose_next_round.md)" < {llm_input} > {next_config}' \
  --llm-cmd-final 'claude -p "$(cat prompts/claude_code/final_summary.md)" < {trajectory} > {final_report}' \
  --out-dir runs/auto_001/ \
  --evaluate-search-path examples/tabular_toy
```

### Codex CLI

```bash
python scripts/round_runner.py auto \
  --config examples/auto_loop/initial_config.yaml \
  --rounds 5 \
  --llm-cmd 'codex exec --output-file {next_config} - < {llm_input}' \
  --llm-cmd-final 'codex exec --output-file {final_report} - < {trajectory}' \
  --out-dir runs/auto_001/
```

The skill quotes substituted paths via `shlex.quote`, so the template
should NOT add its own quotes around placeholders — paths with spaces
are still one token after the shell parses the command.

## Placeholder reference

Per-round (`--llm-cmd`) gets:

- `{llm_input}` — path to `round_NN/llm_input.md`
- `{bundle}` — path to `round_NN/bundle.json`
- `{next_config}` — where to write the next round's config
- `{analysis}` — where to write the LLM's round_report.md (optional)
- `{round_id}`, `{next_round_id}` — string ids

Final (`--llm-cmd-final`) gets:

- `{trajectory}` — path to the multi-round trajectory markdown
- `{final_report}` — where to write the final review
- `{out_dir}` — the run's root directory

## Mechanical fields owned by the skill (the LLM can be sloppy about these)

After the LLM writes `next_config.json`, the skill mechanically
rewrites:

- `round_id` — incremented from the parent round's id.
- `evaluate` — carried forward from the parent config (operator-set).
- `provenance.kind = "llm_proposed"`.
- `provenance.source_round_id` — the parent round's id.
- `provenance.source_bundle_hash` — sha256 of the parent bundle JSON.
- `provenance.parent_config_hash` — sha256 of the parent config JSON.
- `provenance.generated_at` — set if missing.
- `provenance.generated_by.tool = "auto_loop"` (only if the LLM did not set its own tool).

The LLM keeps responsibility for the **scientific** fields: the
`search_space` changes, sampler / pruner choices, `n_trials`, the
free-form `rationale`, and the `diff_summary` rows.

## Token cost

Per-round LLM call sees only that round's `llm_input.md` (~1.4K tokens).
The final call sees a deliberately compact trajectory (per-round
summary stats + global best + per-round coverage notes), NOT a
concatenation of every round's full bundle. Total tokens grow
**linearly** in N: 5 rounds ~25K tokens, 20 rounds ~110K tokens.
