# System Overview

## Why a sidecar workspace

Awareness and NewsImpact are independently-released systems with different
maturity levels:

- Awareness is **release-accepted**. Its DocCapture contract and JSONL
  durability are stable and other teams depend on them.
- NewsImpact is **quarantined**. Its governance metadata records a regressive
  fusion verdict and the release gate is closed.

A destructive merge would either compromise Awareness's clean release semantics
(by mixing in a quarantined module) or compromise NewsImpact's governance (by
losing track of the quarantine boundary in a hybrid repo). The sidecar approach
sidesteps both problems: `fusion_stack` lives in a sibling directory, installs
the upstreams editable, and consumes them through narrow read-only surfaces.

## The data flow

```
                         Awareness (stable)                    NewsImpact (quarantined)
                ┌──────────────────────────────┐               ┌─────────────────────────┐
                │  Workers → JSONL committed   │               │  governance_index.json  │
                │  data/jsonl/captures/Y/M/D/  │               │  (read-only)            │
                │      captures-*.jsonl        │               │  + chart artifacts      │
                └────────────────┬─────────────┘               └────────────┬────────────┘
                                 │                                          │
                            (post-commit)                       (research_diagnostic only)
                                 │                                          │
                                 ▼                                          ▼
                ┌────────────────────────────────────────────────────────────────────────┐
                │                       fusion_stack supervisor                         │
                │                                                                       │
                │   awareness_reader → awareness_replay (offsets, idempotent)           │
                │       │                                                               │
                │       ▼                                                               │
                │   FusionService.process(cap)                                          │
                │       │                                                               │
                │       ├── A. FastPrefilter        (rules + domain priors)             │
                │       ├── B. ZeroShot             (bart-large-mnli OR stub)           │
                │       ├── C. Sentiment            (FinBERT OR stub)                   │
                │       ├── D. Embeddings           (MiniLM OR hashed stub)             │
                │       ├── E. Reranker             (ms-marco OR rapidfuzz)             │
                │       ├── F. EntityLinker / SymbolMapper                              │
                │       ├── G. ChartContext         (metadata only; quarantined-safe)   │
                │       ├── H. Scoring              (transparent component_scores)      │
                │       └──   Evidence              (extractive top-K sentences)        │
                │       │                                                               │
                │       └── Diagnostic stamp (research_diagnostic mode only)            │
                │       │                                                               │
                │       ▼                                                               │
                │   FinancialImpactRecord                                               │
                │                                                                       │
                │   storage (SQLite + parquet + DLQ + offsets + vectors)                │
                │                                                                       │
                │   ┌─ FastAPI ─┐    ┌─ CLI ─┐                                          │
                │   │ /recent   │    │ run   │                                          │
                │   │ /metrics  │    │ ...   │                                          │
                │   └───────────┘    └───────┘                                          │
                └────────────────────────────────────────────────────────────────────────┘
```

## The decision (Stage H)

The `is_finance_relevant` flag is **always** explained by the `component_scores`
map that ships with every record. The transparent weighting is:

```
0.30 * max(asset_class)
+ 0.30 * max(reason_code)
+ 0.15 * prefilter_rule_score
+ 0.10 * domain_prior
+ 0.05 * source_type_prior
+ 0.05 * sentiment_confidence (only when non-neutral)
+ 0.05 * entity_density
```

Plus a **negative-class veto**: if any of `sports / celebrity / lifestyle /
entertainment / general_human_interest / irrelevant_local` exceeds the
`negative_class_block` threshold (default 0.65) *and* asset/reason scores are
below 0.5, the item is forced to `is_finance_relevant = False`.

## Stubs vs ML

Every model-backed stage has two implementations behind a `Protocol`:

| Stage | Stub | ML |
|---|---|---|
| Zero-shot | alias-overlap with hypothesis-derived tokens | facebook/bart-large-mnli |
| Sentiment | finance polarity lexicon | ProsusAI/finbert (or FinBERT-tone, or distilroberta) |
| Embedder | blake2b + token-frequency overlay (96D, cosine-stable) | sentence-transformers/all-MiniLM-L6-v2 |
| Reranker | rapidfuzz token_set_ratio | cross-encoder/ms-marco-MiniLM-L6-v2 |

Stubs are the default. They keep tests CPU-fast and remove the requirement for
HF model downloads. `--with-ml` swaps in real models. Falling back to the stub
on import failure is automatic — the pipeline never crashes because torch is
missing.

## Storage

Three sinks share a single SQLite database for metadata:

- **SQLite** (`data/db/fusion.sqlite3`) — record index, label inverted index,
  offsets, model versions, DLQ.
- **Parquet** (`data/results/records-*.parquet`) — rotated batched exports for
  downstream analytics.
- **Vector cache** (`data/vector_index/<capture_id>.npy`) — per-record embedding
  for clustering / dedupe.

All durable writes happen *after* a successful pipeline pass; failed captures go
to `dlq` and do not advance the offset.

## NewsImpact safety boundary

Three independent checks gate every diagnostic activation:

1. `fusion_stack` mode must be `research_diagnostic`.
2. `guards.newsimpact_diagnostic_enabled` must be `true`.
3. `governance_index.json` must still report `release_gate_passed: false`.

If any check fails, the diagnostic adapter raises `NewsImpactGuardError` and the
supervisor proceeds without it. The diagnostic payload is *metadata only* — it
is forbidden from overriding `is_finance_relevant`.

Read `docs/SOURCE_OF_TRUTH.md` for the full statement.

## Failure modes we tolerate

- Awareness not pip-installed → JSONL replay still works.
- NewsImpact missing entirely → SymbolMapper falls back to the internal
  registry; ChartContextReader returns "unavailable".
- HF cache empty / no internet → stubs run.
- Kaggle credentials missing → downloads skipped with status code 0.
- `final_best.pt` absent → guards still pass because the verifier reads
  `governance_index.json`, not the checkpoint itself.
