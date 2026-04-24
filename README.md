# optuna-round-refinement

A reusable, model-agnostic skill package for **round-level Optuna hyperparameter
refinement with an LLM-in-the-outer-loop**.

> **Core principle.** Optuna is the trial sampler. The LLM is *only* an
> outer-loop analyst that reads a completed round's bundle and proposes the
> *next* round's configuration. The LLM never steers individual trials.

---

## What this skill is

A portable contract (schemas + prompts + templates + docs) for running
hyperparameter studies as a sequence of **rounds**:

```
  Round R                                    Round R+1
  ──────────────────────                     ──────────────────────
  Optuna study     ──► study_bundle.json ──► LLM analyst ──► next_round_config.json ──► Optuna study
  (N trials)             (summary + stats)    (off-line)      (schema-validated)         (N trials)
                                                                    │
                                                            human reviewer (optional)
```

Each round is self-contained, reproducible, and auditable. The LLM sees only
the **summarised study bundle** of a finished round, never live trial metrics.

## Install

### Install as a Claude Code plugin

This repo ships a [`.claude-plugin/`](.claude-plugin/) manifest, so it can be
added as a Claude Code plugin marketplace directly from GitHub:

```shell
/plugin marketplace add sfr9802/optuna-round-refinement
/plugin install optuna-round-refinement@sfr9802-skills
```

The skill is then available in any Claude Code session. To refresh later:

```shell
/plugin marketplace update sfr9802-skills
```

### Install as a Codex context pack

This repo ships an [`AGENTS.md`](AGENTS.md) at the root. Codex automatically
discovers and loads it when you run `codex` from inside a clone of the repo:

```bash
git clone https://github.com/sfr9802/optuna-round-refinement.git
cd optuna-round-refinement
codex
```

To use the skill from your own project, reference the relevant sections of
[`AGENTS.md`](AGENTS.md) and [`prompts/codex/`](prompts/codex/) from your own
project's `AGENTS.md` — Codex layers `AGENTS.md` files from the git root
down to your current directory, so nested or vendored copies compose
automatically.

## Quickstart

After installing the skill (see [Install](#install) above) or vendoring
it into your own repository as `third_party/optuna-round-refinement/`,
the project side contributes **one scoring callable** plus one config
YAML. Everything else (Optuna wiring, bundle export, axis-coverage
enrichment, LLM-input rendering) is owned by the skill package.

1. Read [`SKILL.md`](SKILL.md) for the contract — in particular §1
   Step 2's scan-before-wrap decision tree.
2. **Point at an existing `(params: dict) -> number | dict`-shaped
   function in your project.** File name, function name, and module
   path are all arbitrary — the config's `evaluate: "module:callable"`
   is a dotted path, not a naming rule. `scoring:run_trial`,
   `tests.helpers:_score`, `eval.harness:score_one` all work.
   Only write a new file if the existing eval is CLI-based (argparse +
   subprocess) and a minimal wrapper (~30 LOC) is genuinely required.
   The scoring callable receives the merged dict of sampled
   search-space values and `fixed_params`, and returns either a single
   number (the primary metric) or `{"primary": <number>,
   "secondary": {...}}`.
3. Write a config YAML conforming to
   [`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json)
   with an `evaluate:` pointer:
   ```yaml
   evaluate: "my_module:evaluate"
   direction: "maximize"
   objective_name: "val_auc"
   round_id: "round_01"
   n_trials: 20
   sampler: { type: "TPESampler", params: {}, seed: 42 }
   pruner:  { type: "MedianPruner", params: {} }
   search_space: { ... }
   fixed_params: { ... }
   provenance: { kind: "initial", ... }
   ```
4. Run round 1 with the skill-owned CLI:
   ```bash
   python <skill_root>/scripts/round_runner.py run \
       --config my_config.yaml \
       --out-bundle run_output/study_bundle.json \
       --out-llm-input run_output/llm_input.md
   ```
   This writes a fully-enriched, schema-validated `study_bundle.json`
   and the rendered `llm_input.md` — no project-side adapter code
   required.
5. Feed `llm_input.md` to the LLM using a prompt from
   [`prompts/`](prompts/). The LLM produces a `round_report.md` and a
   draft `next_round_config.json`.
6. Validate the LLM output against
   [`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json),
   freeze it, and run round 2 by pointing the CLI at the new config.
   Repeat until budget or stop-condition is hit.

See [`examples/tabular_toy/`](examples/tabular_toy/) for the end-to-end
"evaluate function only" flow.
See [`examples/rag_example/`](examples/rag_example/) for a domain-level
walkthrough of the round-to-round artifact contract.

### Sleep-mode auto loop

For "set N rounds and let it run overnight," the skill ships an `auto`
subcommand that chains rounds together with an LLM call between each
pair and a study-wide review after the last round. The user MUST
choose the round count (`--rounds`) — there is no default. The hard
cap is `AUTO_LOOP_HARD_CAP = 50` rounds.

```bash
python scripts/round_runner.py auto \
    --config initial.yaml \
    --rounds 5 \
    --llm-cmd 'claude -p "$(cat prompts/claude_code/propose_next_round.md)" < {llm_input} > {next_config}' \
    --llm-cmd-final 'claude -p "$(cat prompts/claude_code/final_summary.md)" < {trajectory} > {final_report}' \
    --out-dir runs/study_001/
```

What you get back:

```
runs/study_001/
├── round_01/{config,bundle,llm_input,analysis,next_config}
├── round_02/...
├── round_05/{config,bundle,llm_input}    ← last round: no LLM transition
├── trajectory.md          ← compact multi-round summary fed to --llm-cmd-final
├── final_report.md        ← LLM's study-wide review
└── summary.md             ← artifact index + global best
```

Token cost is **linear in N**, not exponential — per-round calls see
only that round's bundle (~1.4K tokens), and the final call sees a
deliberately compact trajectory (per-round headline stats + global best
+ per-round coverage notes), not a concatenation of every bundle.

End-to-end runnable demo (no real LLM required):
[`examples/auto_loop/`](examples/auto_loop/).

### Low-level Python API (escape hatch)

For multi-objective studies, distributed storage, custom callbacks, or
any case the CLI doesn't cover, drop down to the library directly:
`scripts/round_adapter.py::build_study_bundle(raw, out_path=...)` and
`render_llm_input(bundle, out_path=...)` own axis-coverage enrichment
and markdown rendering, so a hand-written driver still gets the safer
behaviour without re-implementing it.

## What this skill is *not*

- It is **not** an Optuna replacement. You keep your sampler, pruner, and
  objective function exactly as they are.
- It is **not** a per-trial advisor. Any design that feeds live trial feedback
  to an LLM mid-round is explicitly out of scope and discouraged
  (see [`docs/anti_patterns.md`](docs/anti_patterns.md)).
- It is **not** tied to a specific repository layout. A thin project-side
  adapter (≈ 100 LOC) wires the skill's schemas to your code.

## When to use

Use the skill when **all** of the following hold:

1. You are running an Optuna (or Optuna-compatible) study with a meaningful
   per-trial cost (>seconds) so that spending an LLM call between rounds is
   negligible overhead.
2. You want the search space, sampler, or fixed params to **evolve across
   rounds** based on observed structure (importance, clusters, pruning
   patterns, boundary hits).
3. You can afford to **freeze** the search space inside one round.

Do **not** use it for:

- Cheap, uniform grid/random sweeps where a single large study is simpler.
- Online/bandit-style tuning where decisions must happen per-trial.
- Studies where reproducibility and provenance are not required.

## Repository layout

```
optuna-round-refinement/
├── README.md                       ← this file
├── SKILL.md                        ← machine-readable skill manifest
├── AGENTS.md                       ← Codex context (auto-loaded by Codex CLI)
├── LICENSE                         ← MIT
├── .claude-plugin/
│   ├── plugin.json                 ← Claude Code plugin manifest
│   └── marketplace.json            ← Claude Code marketplace entry
├── docs/
│   ├── design.md                   ← why round-level, outer-loop-only
│   └── anti_patterns.md            ← forbidden usage modes
├── schemas/
│   ├── study_bundle.schema.json    ← input to the LLM
│   └── next_round_config.schema.json ← output from the LLM
├── templates/
│   ├── llm_input.md                ← human-readable bundle rendering
│   ├── round_report.md             ← analyst's written output
│   └── next_round_config.yaml      ← starting point for a config
├── prompts/
│   ├── claude_code/
│   │   ├── analyze_round.md
│   │   ├── propose_next_round.md
│   │   └── validate_config.md
│   └── codex/
│       ├── analyze_round.md
│       └── propose_next_round.md
├── scripts/
│   ├── round_runner.py            ← CLI: runs one Optuna round end-to-end
│   └── round_adapter.py           ← bundle helpers (build/load/render)
└── examples/
    ├── rag_example/                ← RAG pipeline config round
    │   ├── round_01_bundle.json
    │   ├── round_01_llm_input.md
    │   ├── round_01_analysis.md
    │   ├── round_02_config.json            ← schema-valid materialised example
    │   └── round_02_config.template.json   ← LLM-authored form with sentinels
    └── tabular_toy/                ← illustrative PyTorch tabular HPO
        ├── experiment.active.yaml
        ├── evaluate.py             ← the only project-side code a user writes
        ├── model.py
        ├── dataset.py
        ├── study_bundle.json
        ├── summary.md
        └── next_round.yaml
```

## Required inputs

To run a round-to-round transition, the project-side adapter must produce:

- A **study bundle** conforming to [`schemas/study_bundle.schema.json`](schemas/study_bundle.schema.json).
- A filled [`templates/llm_input.md`](templates/llm_input.md) that renders the
  bundle as markdown for the LLM.

## Expected outputs

The LLM round-analyst must produce:

- A **round report** conforming to [`templates/round_report.md`](templates/round_report.md)
  (human-readable rationale).
- A **next-round config** conforming to
  [`schemas/next_round_config.schema.json`](schemas/next_round_config.schema.json),
  including **provenance fields** linking back to the source bundle.

## License

MIT — see [`LICENSE`](LICENSE). Copyright (c) 2026 sfr9802.
