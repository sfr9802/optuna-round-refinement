# Tabular toy — PyTorch HPO example

A small, illustrative round-level HPO example built on **PyTorch** and
Optuna, produced as a portability demonstration: the round-level
refinement artifact flow (`study_bundle.json` → analyst report →
`next_round_config`) is a framework-agnostic artifact contract, and this
example shows that it accepts a second unrelated domain (PyTorch tabular
HPO) without schema changes.

> **Scope.** This is an *illustrative* example. It is **not** a claim of
> broad ML/DL empirical validation, a benchmark, or a production-ready
> deep learning HPO setup. It exists to show that the same round-level
> artifact contract used by the RAG example is framework-agnostic and
> can carry a PyTorch tabular study without changes to the schemas,
> prompts, or templates.

## What this example demonstrates

- A small PyTorch MLP trained on a public tabular binary-classification
  dataset (sklearn's `load_breast_cancer`).
- A compact, believable tabular-DL HPO search space: `hidden_units`,
  `num_layers`, `dropout`, `learning_rate`, `batch_size`, `optimizer`,
  `activation`.
- A `train_eval.py` entrypoint that can either train one model given
  hyperparameters or run a full Optuna round from a declarative YAML
  config.
- A round 01 → round 02 walkthrough with a sample
  [`study_bundle.json`](study_bundle.json), analyst
  [`summary.md`](summary.md), and proposed
  [`next_round.yaml`](next_round.yaml) — the same three artifacts the
  RAG example produces, with the same provenance requirements.

## What this example does **not** claim

- It does **not** claim that the round-level refinement skill improves
  deep learning HPO in general.
- It does **not** claim production-readiness for any tabular or deep
  learning domain.
- It does **not** benchmark PyTorch against any other framework.
- It does **not** validate the analyst's proposed round 02 against a
  held-out test set — the sample artifacts are hand-crafted to exercise
  the schema, not to ship a measured result.

The breast-cancer dataset is chosen because it is small, reproducible,
bundled with sklearn, and already close to ceiling for a well-tuned MLP.
It is the right size for a readable demo; it is **not** the right size
for drawing generalisation conclusions about HPO strategies.

## Files

```
examples/tabular_toy/
├── README.md                ← this file
├── experiment.active.yaml   ← active round config (next_round_config shape)
├── train_eval.py            ← per-trial training + Optuna study runner
├── model.py                 ← SimpleMLP for tabular inputs
├── dataset.py               ← loads + scales breast_cancer split
├── study_bundle.json        ← sample round_01 bundle (hand-crafted)
├── summary.md               ← sample round_01 → round_02 analyst report
└── next_round.yaml          ← sample round_02 proposed config
```

## Running a round locally

### Dependencies

This example depends on, beyond the skill package itself:

- `python>=3.9`
- `torch` (CPU build is sufficient)
- `optuna>=3.0`
- `numpy`
- `scikit-learn`
- `pyyaml`

None of these are pinned by the skill package; install them in your own
environment. GPU is not required.

### Run round 01

```bash
cd examples/tabular_toy
python train_eval.py --config experiment.active.yaml --out run_output/study_bundle.json
```

This writes the run's bundle to `run_output/study_bundle.json`, which is
covered by the repo's top-level `.gitignore` (`**/run_output/`). 20 trials
should finish in a few minutes on a modern laptop CPU.

> **Preserve the checked-in sample artifacts.** The `study_bundle.json`,
> `summary.md`, and `next_round.yaml` files in this directory are
> hand-crafted illustrative samples, not outputs of a real run. Keep the
> run output under `run_output/` (or another gitignored path) so the
> checked-in samples stay intact for readers. Do **not** pass
> `--out study_bundle.json`; that would overwrite the checked-in sample.

### Produce round 02 with an analyst

The round 02 transition follows the same workflow as the RAG example:

1. Render [`study_bundle.json`](study_bundle.json) into markdown with
   [`../../templates/llm_input.md`](../../templates/llm_input.md) using
   your project-side adapter.
2. Run the analyst prompt from
   [`../../prompts/claude_code/propose_next_round.md`](../../prompts/claude_code/propose_next_round.md)
   (or the Codex variant) on that rendered markdown.
3. Validate the resulting next-round config against
   [`../../schemas/next_round_config.schema.json`](../../schemas/next_round_config.schema.json).
4. Have a human reviewer approve the config if any "large change" flags
   were raised (see [`../../docs/anti_patterns.md#a7`](../../docs/anti_patterns.md)).

The sample [`summary.md`](summary.md) + [`next_round.yaml`](next_round.yaml)
in this directory show what step 2–3 should produce for the sample
bundle.

## What was inspired by a prior notebook experiment

The problem shape — binary classification on a tabular dataset with a
small MLP tuned via random HPO knobs such as `batch_size`,
`learning_rate`, and `dropout` — was drawn from a prior TensorFlow/Keras
notebook that explored the Spaceship Titanic dataset.

The PyTorch example here does **not** port that notebook. It reimplements
the general idea in a portable, deterministic, CPU-friendly form. In
particular:

- **Framework.** Rewritten in PyTorch. TensorFlow/Keras is not required
  and not installed anywhere in this skill package.
- **Dataset.** Replaced with `sklearn.datasets.load_breast_cancer` for
  reproducibility and zero external data dependencies. The notebook's
  competition-specific preprocessing (cabin parsing, name handling,
  outlier flags, etc.) was intentionally dropped.
- **Model.** Reduced to a single readable MLP class with a configurable
  number of layers, hidden size, dropout, and activation. The notebook's
  multi-input concatenation architectures were not preserved.
- **Training loop.** Replaced with a minimal explicit PyTorch loop so the
  example is easy to read as a single file.
- **Evaluation.** Primary metric is `val_auc`, secondaries are
  `val_accuracy`, `train_time_s`, and `n_params`. The notebook's
  `binary_accuracy` / `binary_crossentropy` are analogous but not
  reproduced as a matter of framework fidelity.

## Positioning inside the skill

The `rag_example/` and `tabular_toy/` directories together show two
different domains (RAG pipeline configuration, PyTorch tabular HPO)
producing the **same three artifacts** against the **same two JSON
schemas**. That is the only claim this example makes: the artifact
contract is framework-agnostic and designed to generalise — this toy
serves as an illustrative portability demonstration, not evidence that
it does. Anything further — that the analyst's proposals generalise
well, that the skill is production-safe for deep learning HPO, or that
round-level refinement beats any particular baseline — is out of scope
here.

Only the `rag_example/` workflow is treated as the currently validated
example. The `tabular_toy/` directory is illustrative only.
