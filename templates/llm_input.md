<!--
  Template: llm_input.md
  Purpose:  Render one StudyBundle as markdown for the LLM round-analyst.

  Rendering:
    - The skill package ships the canonical renderer at
      scripts/round_adapter.py::render_llm_input. It fills every
      {{placeholder}} in this file AND resolves the coverage-note column
      in §4 from the bundle's pre-baked statistics.axis_coverage.<p>.note
      (or recomputes it on the fly for legacy bundles). Downstream
      adapters do NOT need to author handlebars helpers, implement a
      coverage_note function, or extend the template context.
    - Projects that want to use an alternative rendering engine may do
      so, but {{this.note}} is always present in the bundle when
      produced through the skill's canonical build path, so no extra
      helpers are required.

  Rules:
    - Do NOT insert raw training data, logs, or user text. Summaries only.
    - Keep the total size under ~30 KB so the LLM sees all of it in one pass.
-->

# Round {{round_id}} — study bundle

**Study:** `{{study_id}}`
**Objective:** `{{objective.name}}` ({{objective.direction}})
**Optuna:** `{{optuna.version}}` | sampler `{{optuna.sampler.type}}` | pruner `{{optuna.pruner.type}}`
**Parent config hash:** `{{parent_config_hash | default:"(none — initial round)"}}`
**Bundle hash:** `{{bundle_hash}}`

---

## 1. Frozen search space (this round)

| Param | Type | Range / choices | Log | Step |
|-------|------|-----------------|-----|------|
{{#each search_space}}
| `{{@key}}` | {{this.type}} | {{this.range_or_choices}} | {{this.log}} | {{this.step}} |
{{/each}}

Fixed params: `{{fixed_params | json}}`

## 2. Headline statistics

- Trials: **{{n_trials}}** (complete {{statistics.n_complete}}, pruned {{statistics.n_pruned}}, failed {{statistics.n_failed}})
- Best value: **{{statistics.best_value}}**
- Quantiles: p10 `{{statistics.quantiles.p10}}`, p50 `{{statistics.quantiles.p50}}`, p90 `{{statistics.quantiles.p90}}`
- Mean ± std: `{{statistics.mean_value}} ± {{statistics.std_value}}`

## 3. Param importances

{{#if param_importances}}
| Param | Importance |
|-------|-----------|
{{#each param_importances_sorted}}
| `{{this.name}}` | {{this.value}} |
{{/each}}
{{else}}
_No importances computed this round._
{{/if}}

## 4. Boundary hits & axis coverage

> **Read this carefully.** `boundary_hits.<p>.high = 0` alone is AMBIGUOUS.
> It can mean *"upper edge was sampled and performed poorly"* OR *"upper
> edge was never sampled"*. These two cases must be handled differently.
> The `axis_coverage` columns below disambiguate them. If `axis_coverage`
> is absent from the bundle, coverage is **unknown** — do NOT use
> `boundary_hits` alone to justify narrowing.

{{#if statistics.axis_coverage}}
For each numeric param: configured range, sampled range (over valid
COMPLETE trials), unique sampled values, boundary hits, and a coverage
note baked in by the skill package. An **UNSAMPLED EDGE** means no
COMPLETE trial reached that edge of the configured range.

| Param | Configured | Sampled (min…max) | Unique | Boundary hits (low / high) | Coverage note |
|-------|------------|-------------------|--------|----------------------------|---------------|
{{#each statistics.axis_coverage}}
| `{{@key}}` | `{{lookup ../search_space @key "range_or_choices"}}` | `{{this.sampled_min}} … {{this.sampled_max}}` | {{this.unique_count}} | {{lookup ../statistics.boundary_hits @key "low"}} / {{lookup ../statistics.boundary_hits @key "high"}} | {{this.note}} |
{{/each}}

The `note` field is populated automatically by the skill's canonical
bundle path (`scripts/round_adapter.py::compute_axis_coverage`, invoked
by `build_study_bundle` / `load_study_bundle` / `normalize_study_bundle`
and by the shipped renderer `render_llm_input`). Downstream adapters do
not implement the classification. Values the skill produces:

- `sampled_max < configured_high`  → **"upper edge UNSAMPLED"**
- `sampled_min > configured_low`   → **"lower edge UNSAMPLED"**
- both edges UNSAMPLED             → **"lower edge UNSAMPLED; upper edge UNSAMPLED"**
- both sides at the configured edges → **"full coverage"**
- `unique_count == 0`              → **"no valid completes — coverage unknown for this axis"**

If a row shows **"upper edge UNSAMPLED"** or **"lower edge UNSAMPLED"**,
treat `boundary_hits.<p>.<side> = 0` on that side as *lack of evidence*,
not as *negative evidence*. Do **not** cite it as a reason to narrow.

{{else}}
Only legacy `boundary_hits` is available in this bundle — **coverage is
unknown**. Treat every `boundary_hits.<p>.<side> = 0` as ambiguous. The
analyst MUST NOT use `boundary_hits` alone to justify narrowing in this
bundle; prefer HOLD, a random-sampler exploration round, or an explicit
EXPAND / RE-OPEN. See `docs/anti_patterns.md#a10`.

| Param | Boundary hits (low / high) |
|-------|----------------------------|
{{#each statistics.boundary_hits}}
| `{{@key}}` | {{this.low}} / {{this.high}} |
{{/each}}
{{/if}}

## 5. Best trial

```json
{{best_trial | json_pretty}}
```

## 6. Top-k trials (k={{top_k | default:10}})

| # | value | params |
|---|-------|--------|
{{#each top_trials}}
| {{this.number}} | {{this.value}} | `{{this.params | json}}` |
{{/each}}

## 7. Clusters (optional)

{{#if clusters}}
{{#each clusters}}
- **{{this.label}}** — trials {{this.trial_numbers | join:", "}}
{{/each}}
{{else}}
_No clusters provided._
{{/if}}

## 8. Operator notes

{{notes | default:"(none)"}}

---

## Your task

You are the outer-loop analyst. Read the bundle above and produce:

1. A **round report** (see `templates/round_report.md`).
2. A **next-round config** JSON conforming to `schemas/next_round_config.schema.json`.

**Hard constraints:**

- Do NOT propose per-trial steering or mid-round changes.
- Do NOT emit Python or Optuna API calls — declarative JSON only.
- Every `search_space` change must cite a specific field of this bundle in
  `provenance.rationale` and `provenance.diff_summary[*].evidence`.
- Fill all required `provenance` fields. Use
  `provenance.kind = "llm_proposed"`.
- **Never narrow against an UNSAMPLED EDGE** (see section 4 and
  `docs/anti_patterns.md#a10`). If `axis_coverage` is absent, treat
  coverage as unknown and do NOT use `boundary_hits` alone to justify
  narrowing.
