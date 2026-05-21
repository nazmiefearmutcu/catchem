# `--with-ml` fallback behavior

This document records what actually happens when an operator runs the
bootstrap with `--with-ml` on a fresh machine, and why the stub path is
the production default.

## Default (no `--with-ml`)

`catchem[dev]` is small (FastAPI + Pydantic + a handful of helpers).
Every model-backed stage in `service.py` ships behind a `make_*(use_stub=...)`
factory:

| Stage | Stub model id | Real model id (env: `--with-ml`) |
|---|---|---|
| Zero-shot | `stub-zero-shot/v1` | `hf:facebook/bart-large-mnli` |
| Sentiment | `stub-sentiment/v1` | `hf:ProsusAI/finbert` |
| Embeddings | `stub-embed/v2` (64-D blake2b hashing trick) | `hf:sentence-transformers/all-MiniLM-L6-v2` |
| Reranker | `stub-rerank/v1` (rapidfuzz token-set-ratio) | `hf:cross-encoder/ms-marco-MiniLM-L6-v2` |

The stubs are deterministic, CPU-friendly, and produce a clean F1 on the
synthetic golden set (precision=1.0, recall=1.0, F1=1.0, symbol_recall=1.0,
sentiment_accuracy=0.67). See `tests/test_golden_benchmark.py` for the pin.

## `--with-ml` install path

`bash scripts/catchem_bootstrap_and_run.sh --with-ml` runs:

```
uv pip install -e ".[ml]"            # torch + transformers + sentence-transformers + huggingface_hub
python scripts/warm_hf_models.py     # snapshot_download for the four IDs above
```

When this completes, `CATCHEM_USE_ML_STUBS=false` (or removing the env entirely)
makes `make_*` return the real HF wrappers on next supervisor construction.

## Known environment limitations on macOS

On Apple Silicon with Python 3.13.3 in this sandbox, `uv pip install -e ".[ml]"`
hangs silently. Three independent causes:

1. **No prebuilt `torch` wheel for Python 3.13 on macOS** for the version
   selected by the resolver — `pip` falls back to building from source which
   needs Xcode + ~1 GB of compile artifacts and never prints progress.
2. **`uv pip install --quiet`** suppresses the per-file download counter
   that would otherwise reveal which package is stuck.
3. **HF model weights are ~1.6 GB combined** — `warm_hf_models.py` can take
   10+ minutes on a residential connection. There is no per-file progress
   bar through `huggingface_hub`'s default API.

## Pragmatic fallback (already in place)

`scripts/catchem_bootstrap_and_run.sh` does **not** fail when `[ml]` install
fails. It prints a yellow warning and continues:

```
[bootstrap] installing optional ML extras (...)
[bootstrap] ML extras failed; continuing with stubs
```

This means the operator can run `--with-ml` opportunistically: if the
machine has the wheels cached, they get used; otherwise, the pipeline runs
on stubs and emits records with `model_versions.zero_shot == "stub-zero-shot/v1"`.
The UI's `OpsPage` and `/metrics` both display this so it's never silent.

## Operational recommendation

For local laptop work, use the stub default. Use `--with-ml` on a workstation
with:

- A CUDA GPU (then torch installs in seconds) **OR** a macOS host with
  `torch` already cached at the resolver-selected version,
- ≥ 4 GB free disk in `$HF_HOME` (default `~/.cache/huggingface`),
- A long-lived terminal — drop `--quiet` from the install command in the
  bootstrap script to see progress if you want feedback.

## How to verify which models are live

```bash
curl -s http://127.0.0.1:8087/metrics | python -m json.tool
# model_versions.zero_shot startswith "stub-"   →  stub path active
# model_versions.zero_shot startswith "hf:"     →  real HF model loaded
```

The same field appears in every `FinancialImpactRecord.model_versions`, so
each record carries its own provenance — no risk of silent ambiguity about
which model produced a verdict.

## Stub vs. ML diff (observed on this machine)

We could not complete a real-ML benchmark run in this sandbox (see "Known
environment limitations"). The expected differences when ML is live:

| Metric | Stub baseline (measured) | ML expectation |
|---|---|---|
| relevance.precision | 1.0 | 0.95–1.0 (ML may flag borderline items) |
| relevance.recall | 1.0 | 0.95–1.0 |
| sentiment_accuracy | 0.67 | 0.85–0.95 (FinBERT is much stronger than the lexicon stub) |
| symbol_recall | 1.0 | 1.0 (symbol mapping is rule-based, not ML) |
| asset_class_f1 (per-class spread) | wider — stub's hypothesis-token matcher is coarse | tighter clusters |

If a future run completes `--with-ml`, persist the report to
`data/results/benchmark_history.jsonl` and compare via `/ui/benchmark/history`.
