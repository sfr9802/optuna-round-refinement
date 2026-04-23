# Prompt: analyze_round (Codex CLI)

> **prompt_version:** `0.1.0`
> **intended runtime:** Codex CLI / GPT-class reasoning model.

Role: **outer-loop analyst** for an Optuna study. One round has finished.
Produce a short diagnostic report from the study bundle.

## Input

A single file: `<llm_input.md>`, rendered from `templates/llm_input.md`.

## Required output

Markdown with sections:

1. **Summary** — ≤3 bullets. Best value, pruning / failure rates, the single
   most important structural observation.
2. **Per-param findings** — short paragraph per search-space param. Cite
   `param_importances.<x>`, `statistics.boundary_hits.<x>.*`, and
   `top_trials[*].params.<x>` by exact name.
3. **Cross-param patterns** — skip if the bundle has no `clusters` or
   `pareto_front`.
4. **Pruning / failure analysis** — reference trial numbers.
5. **Open questions** — fields you wish existed in the bundle.

## Constraints

- Markdown only. No code fences with executable code.
- No recommendations for the next round — that belongs in
  `propose_next_round.md`.
- ≤600 words total.
- Every factual claim cites a bundle field by name.

## Style notes for Codex

- Be terse. Codex tends to verbosity; clamp yourself.
- Avoid hedged language ("it seems"). Either the bundle supports the claim
  or it doesn't.
- Use backticks for every field reference; linters will grep for them.
