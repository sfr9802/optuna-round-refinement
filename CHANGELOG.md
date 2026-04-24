# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.3.3

### Added
- **`/refine` and `/refine-auto` slash commands.** When the skill is
  installed as a Claude Code plugin, two explicit slash commands are
  now available:
  - `/refine <config_path> [N]` — one round when `N` is omitted or
    equals 1; routes to the auto-loop when `N >= 2`. Single-round
    mode lets the current Claude Code session drive the analysis
    interactively; auto-loop mode spawns fresh headless `claude
    --print` subprocesses for every round-to-round transition so
    the outer session stays responsive.
  - `/refine-auto <config_path> <N>` — explicit alias that always
    runs the auto-loop, with `N` required. Use when you want the
    multi-round intent to be visible in the command itself.
  Both commands live in `commands/` at the plugin root and resolve
  `${CLAUDE_PLUGIN_ROOT}` at runtime, so they work regardless of
  where the plugin is cached.
- SKILL.md now documents the slash-command entry points alongside
  the existing CLI entry points.

### Changed
- README "Install" section surfaces the two slash commands with
  copy-pastable examples right after the `/plugin install` step.

### Compatibility
- Fully additive. The underlying `run` / `auto` CLI and Python API
  are unchanged; the slash commands are thin orchestration wrappers.
  Installations that never invoke a slash command (e.g. vendored
  `third_party/optuna-round-refinement/` without the plugin
  manifest) are unaffected.

## v0.3.2

### Added
- **Sleep-mode auto loop (`auto` subcommand).** `python scripts/round_runner.py auto
  --config <initial> --rounds <N> --llm-cmd '<tpl>' [--llm-cmd-final '<tpl>']
  --out-dir <dir>` runs N Optuna rounds back-to-back, shelling out to the user's
  LLM between each pair of rounds to produce the next-round config and
  (optionally) once more after the last round for a study-wide review.
  `--rounds` is a required flag with no default — the user must choose N
  deliberately. A hard cap of `AUTO_LOOP_HARD_CAP = 50` prevents runaway
  studies. Failed LLM invocations retry once by default and the loop
  preserves all prior-round artifacts on the final failure so the user
  can resume manually.
- **`render_study_trajectory()` in `scripts/round_adapter.py`** — new canonical
  renderer that turns N normalised bundles + per-round analyses into a compact
  multi-round markdown (headline stats table, search-space evolution, importance
  drift, global best trial, per-round coverage notes, prior LLM analyses). This
  is the input to `--llm-cmd-final`; token cost is linear in N, not a
  concatenation of every bundle.
- **Mechanical-field injection for LLM-produced next configs.** The runner
  silently rewrites `round_id`, `evaluate`, and `provenance.{kind,
  source_round_id, source_bundle_hash, parent_config_hash, generated_at}` after
  loading the LLM's output, so the LLM can be sloppy about mechanical fields
  and still produce schema-valid configs. The LLM owns only the scientific
  fields (`search_space`, sampler/pruner, `rationale`, `diff_summary`).
- **Final-summary prompts** — `prompts/claude_code/final_summary.md` and
  `prompts/codex/final_summary.md` for the `--llm-cmd-final` step. Both enforce
  "markdown only, no JSON config" and require quantitative claims to cite a
  trajectory field.
- **Runnable auto-loop demo** — `examples/auto_loop/` with `initial_config.yaml`,
  `stub_llm.py` (offline stub so CI can exercise the full N-round flow without
  a real LLM call), and a README documenting placeholder substitution, real-LLM
  invocation patterns for Claude Code and Codex CLI, and the mechanical-field
  contract.
- **Auto-loop smoke tests** — `tests/test_auto_loop.py` covers the 3-round
  artifact tree, schema-valid provenance on every transition, `--rounds`
  validation, hard-cap enforcement, and clean failure with artifact preservation
  on a failing `--llm-cmd`.

### Changed
- `SKILL.md` gains a `§1A` "Sleep-mode workflow" section documenting the
  `auto` subcommand, placeholder reference, token-cost model, and the
  "never negotiate the user's `--rounds` down" guidance. A new trigger
  phrase (자동으로 돌리고 자고 일어나서 결과 확인) routes multi-round
  requests to `auto` instead of the per-round §1 walkthrough.
- `README.md` gains a "Sleep-mode auto loop" section between the
  per-round Quickstart and the low-level Python API.

### Compatibility
- Fully additive. Existing `run` / `render` subcommands, configs, and
  bundles are unchanged. The new `auto` subcommand is opt-in.

## v0.3.1

### Changed
- **SKILL.md Step 2 rewritten** as a mandatory four-branch decision
  tree: (2a) scan the project for an existing
  `(params: dict) -> number | dict`-shaped function and point at it
  directly; (2b) write a minimal wrapper ONLY when the existing eval
  is CLI-shaped (argparse + subprocess, singleton-settings loader,
  async pipeline that can't run in-process); (2c) ask the user to
  supply a callable when the scan finds nothing, and **abort the
  entire workflow** (Steps 3–8 do not run) if the user can't supply
  one; (2d) verify the pointer imports and is callable before Step 3.
  The scan-before-wrap ordering and the explicit abort gate prevent
  the skill from silently inventing an objective or coaxing users
  into creating a new `evaluate.py` when existing code suffices.
- Clarified in the SKILL.md TL;DR, the tabular_toy README, and the
  root README that `evaluate.py` / the `evaluate()` function name
  are **arbitrary** — the config's `evaluate: "module:callable"` is a
  dotted path, not a naming rule. `scoring:run_trial`,
  `tests.helpers:_score`, `eval.harness:score_one` all work. The
  tabular_toy file/function name is readability convenience, not a
  convention adopters must follow.
- Root README Quickstart step 2 now leads with "point at an existing
  function" rather than "write an evaluate function", aligning the
  user-facing quickstart with the SKILL.md decision tree.

### Compatibility
- No code, schema, or on-disk artefact changes. Purely a workflow /
  documentation clarification release. Existing v0.3.0 configs and
  bundles continue to validate and run unchanged.

## v0.3.0

### Added
- `scripts/round_runner.py` — skill-owned CLI + Python orchestration
  for running one full Optuna round end-to-end from a declarative YAML
  config. Owns sampler / pruner construction, `trial.suggest_*`
  dispatch, bundle export, delegation to `build_study_bundle`, and
  optional `render_llm_input` rendering.
  - `run_round(config, out_bundle=…, out_llm_input=…)` — Python entry
    point.
  - `python scripts/round_runner.py run --config <cfg>` — CLI entry
    point, with `--out-bundle`, `--out-llm-input`, and
    `--evaluate-search-path` flags.
  - `python scripts/round_runner.py render --bundle <b>` — re-render
    an existing bundle through `templates/llm_input.md` without
    running a new study.
- `schemas/next_round_config.schema.json` now carries four optional
  operator-set top-level fields:
  - `evaluate` — dotted-path pointer `"module:callable"` to the
    project's evaluate function.
  - `direction` — `"maximize"` (default) or `"minimize"`.
  - `objective_name` — human-readable metric name surfaced in the
    bundle's `objective.name`.
  - `study_id` — optional Optuna study name; defaults to
    `"round_<round_id>"` when absent.

### Changed
- **Project-side contract reduced from "thin adapter" to "one
  callable".** Adopters now contribute only an
  `evaluate(params: dict) -> dict | float` function plus a config YAML
  with `evaluate: "module:callable"`. The skill-owned runner handles
  every other step that used to require ~100 LOC of adapter code.
- `SKILL.md` rewritten as a Claude-Code-native skill contract: an
  8-step workflow (locate config → verify evaluate pointer → run round
  → read bundle → analyse → propose → validate+freeze → hand back)
  that an LLM agent can execute directly.
- Prompt versions bumped to `0.2.0`
  (`prompts/claude_code/propose_next_round.md`,
  `prompts/codex/propose_next_round.md`) with new guidance that
  operator-set top-level fields (`evaluate`, `direction`,
  `objective_name`, `study_id`) MUST be carried forward from the
  parent config unchanged.
- `examples/tabular_toy/` refactored to the new contract:
  - Removed `train_eval.py` (the old thin-adapter driver).
  - Added `evaluate.py` — the sole project-side file, containing just
    the `evaluate(params)` function.
  - Updated `experiment.active.yaml` to add the `evaluate:`,
    `direction:`, `objective_name:` fields.
  - README now shows `python scripts/round_runner.py run …` as the
    one-line round invocation.
- `docs/design.md` §5 replaced "Adapter pattern" with "Project-side
  contract (zero adapter)" and documents the low-level
  `build_study_bundle` / `load_study_bundle` / `render_llm_input`
  escape hatch for multi-objective, distributed, or custom-callback
  setups.
- `AGENTS.md` now documents the zero-adapter contract and the
  carry-forward rule for operator-set fields.

### Compatibility
- Fully backward-compatible on the wire. Existing bundles and configs
  still validate; the four new config fields are all optional.
- The low-level Python API (`build_study_bundle`,
  `load_study_bundle`, `render_llm_input`, `inject_axis_coverage`,
  `compute_axis_coverage`, `normalize_study_bundle`,
  `write_study_bundle`) is unchanged. Projects that drove bundles by
  hand in v0.2.0 can keep doing so.
- Configs written in v0.2.0 that lack `evaluate:` remain schema-valid;
  they just need the field added (or the runner invoked with a
  pre-resolved callable) before they can be run via the new CLI.

## v0.2.0

### Fixed
- Fixed an ambiguity where `statistics.boundary_hits.<p>.high = 0`
  could mean either "weak sampled boundary" or "unsampled boundary".
- Added internally generated `statistics.axis_coverage` to distinguish
  the sampled range from the configured range on every numeric param.
- Prevented narrow recommendations from relying on `boundary_hits`
  alone — the NARROW guardrails in the proposer prompts now require
  joint citation of `axis_coverage` and the PRUNED/FAIL state of any
  cited boundary trials (see `docs/anti_patterns.md#a10`).
- Added conservative handling for legacy bundles without coverage
  information: loaders do not fabricate `axis_coverage` from a
  possibly-partial `trials` list, and the rendered bundle surfaces
  "coverage unknown" so the LLM cannot regress to the pre-fix
  behaviour.

### Changed
- Canonical bundle and rendering flow now runs through skill-owned
  entry points in `scripts/round_adapter.py`:
  - `build_study_bundle(raw, out_path=…, validate=True)` — constructs
    + normalises + validates + (optionally) writes a fresh bundle.
  - `load_study_bundle(path, recompute=False)` — reads + safe-normalises
    (tops up coverage notes without stomping trusted coverage values).
  - `write_study_bundle(bundle, out_path)` — normalise + validate + write.
  - `normalize_study_bundle(bundle)` — safe top-up used internally.
  - `render_llm_input(bundle, out_path=…)` — fills
    `templates/llm_input.md` AND resolves the coverage-note column
    inside the package.
- Coverage notes are generated inside the package and rendered as a
  plain `{{this.note}}` field — no custom template/Handlebars helper
  on the downstream side.
- Added an optional `note` string field to each
  `statistics.axis_coverage.<p>` entry in the bundle schema.
- Updated prompts (Claude Code + Codex, analyze + propose), templates,
  examples, and design/anti-pattern docs to label unsampled edges
  explicitly.
- Split the RAG example config into two artefacts to clarify the
  placeholder policy:
  - `examples/rag_example/round_02_config.json` — schema-valid
    materialised example with real sha256 provenance hashes.
  - `examples/rag_example/round_02_config.template.json` — LLM-authored
    form retaining `"__FILL_AT_ADAPTER__"` sentinels (not expected to
    validate).

### Compatibility
- `statistics.boundary_hits` remains unchanged on the wire.
- Legacy bundles without `statistics.axis_coverage` remain schema-valid
  and are treated conservatively ("coverage unknown") end-to-end.
- Downstream projects do **not** need custom adapters or template
  helpers to pick up the safer boundary handling — upgrading this
  package is sufficient. The v0.1.0 `inject_axis_coverage` helper is
  retained for backward compatibility and now also populates the
  per-param `note` field automatically.

## v0.1.0

- Initial public release: schemas, prompts, templates, docs, and the
  RAG/runtime tuning example. See
  [`.claude/release_notes_v0.1.0.md`](.claude/release_notes_v0.1.0.md)
  for details.
