<!--
  Template: llm_input.md
  Purpose:  Render one StudyBundle as markdown for the LLM round-analyst.
  Rules:
    - Every {{placeholder}} is filled by the project adapter, not by hand.
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

## 4. Boundary hits

Trials that landed at the low / high edge of each param's range:

{{#each statistics.boundary_hits}}
- `{{@key}}`: low={{this.low}}, high={{this.high}}
{{/each}}

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
