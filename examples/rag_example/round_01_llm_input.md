# Round round_01 — study bundle

**Study:** `rag_quality_round_01`
**Objective:** `rag_answer_f1` (maximize)
**Optuna:** `3.6.1` | sampler `TPESampler` | pruner `MedianPruner`
**Parent config hash:** (none — initial round)
**Bundle hash:** `<PLACEHOLDER — illustrative only; recompute from the canonicalised bundle JSON>`

---

## 1. Frozen search space (this round)

| Param | Type | Range / choices | Log | Step |
|-------|------|-----------------|-----|------|
| `top_k` | int | [1, 20] | false | — |
| `chunk_size` | int | [128, 1024] | false | 64 |
| `chunk_overlap` | float | [0.0, 0.5] | false | — |
| `rerank_model` | categorical | `["none", "bge-reranker-base", "bge-reranker-large"]` | — | — |
| `temperature` | float | [0.0, 1.0] | false | — |
| `embed_model` | categorical | `["text-embedding-3-small", "text-embedding-3-large", "bge-m3"]` | — | — |

Fixed params: `{ "hybrid_alpha": 0.5 }`

## 2. Headline statistics

- Trials: **40** (complete 32, pruned 6, failed 2)
- Best value: **0.671**
- Quantiles: p10 `0.462`, p50 `0.548`, p90 `0.658`
- Mean ± std: `0.541 ± 0.062`

## 3. Param importances

| Param | Importance |
|-------|-----------|
| `rerank_model` | 0.41 |
| `embed_model` | 0.27 |
| `chunk_size` | 0.14 |
| `top_k` | 0.09 |
| `temperature` | 0.06 |
| `chunk_overlap` | 0.03 |

## 4. Boundary hits

Trials that landed at the low / high edge of each param's range:

- `top_k`: low=0, high=3
- `chunk_size`: low=1, high=2
- `chunk_overlap`: low=2, high=3
- `temperature`: low=6, high=1

## 5. Best trial

```json
{
  "number": 31,
  "state": "COMPLETE",
  "value": 0.671,
  "params": {
    "top_k": 10,
    "chunk_size": 640,
    "chunk_overlap": 0.2,
    "rerank_model": "bge-reranker-large",
    "temperature": 0.0,
    "embed_model": "text-embedding-3-large"
  }
}
```

## 6. Top-k trials (k=10)

| # | value | params |
|---|-------|--------|
| 31 | 0.671 | `{top_k:10, chunk_size:640, chunk_overlap:0.20, rerank_model:"bge-reranker-large", temperature:0.0,  embed_model:"text-embedding-3-large"}` |
| 38 | 0.668 | `{top_k:11, chunk_size:576, chunk_overlap:0.20, rerank_model:"bge-reranker-large", temperature:0.05, embed_model:"text-embedding-3-large"}` |
| 23 | 0.659 | `{top_k:10, chunk_size:704, chunk_overlap:0.25, rerank_model:"bge-reranker-large", temperature:0.0,  embed_model:"text-embedding-3-large"}` |
| 18 | 0.642 | `{top_k:12, chunk_size:576, chunk_overlap:0.20, rerank_model:"bge-reranker-large", temperature:0.1,  embed_model:"text-embedding-3-large"}` |
| 12 | 0.631 | `{top_k:10, chunk_size:640, chunk_overlap:0.20, rerank_model:"bge-reranker-large", temperature:0.0,  embed_model:"text-embedding-3-large"}` |
| 7  | 0.604 | `{top_k:8,  chunk_size:512, chunk_overlap:0.15, rerank_model:"bge-reranker-large", temperature:0.1,  embed_model:"text-embedding-3-large"}` |
| 0  | 0.512 | `{top_k:5,  chunk_size:512, chunk_overlap:0.20, rerank_model:"none",               temperature:0.7,  embed_model:"text-embedding-3-small"}` |
| 1  | 0.481 | `{top_k:3,  chunk_size:256, chunk_overlap:0.10, rerank_model:"bge-reranker-base",  temperature:0.3,  embed_model:"text-embedding-3-small"}` |

## 7. Clusters

- **rerank_large + embed_large cluster** — trials 7, 12, 18, 23, 31, 38

## 8. Operator notes

First round: wide sweep across 6 params, 40 trials. Reranker choice
dominates; best region is `bge-reranker-large` + `text-embedding-3-large`
with low temperature.

---

## Your task

You are the outer-loop analyst. Read the bundle above and produce:

1. A **round report** (see `templates/round_report.md`).
2. A **next-round config** JSON conforming to
   `schemas/next_round_config.schema.json`.

**Hard constraints:**

- Do NOT propose per-trial steering or mid-round changes.
- Do NOT emit Python or Optuna API calls — declarative JSON only.
- Every `search_space` change must cite a specific field of this bundle in
  `provenance.rationale` and `provenance.diff_summary[*].evidence`.
- Fill all required `provenance` fields. Use
  `provenance.kind = "llm_proposed"`.
