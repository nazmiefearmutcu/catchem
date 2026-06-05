"""Pipeline orchestration: takes one AwarenessCaptureView, runs all stages,
produces one FinancialImpactRecord. The supervisor wraps this for batch/live."""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Iterable, Mapping
from concurrent.futures import BrokenExecutor, Executor

try:
    from concurrent.futures import BrokenProcessPool
except ImportError:  # pragma: no cover
    try:
        from concurrent.futures.process import BrokenProcessPool
    except ImportError:
        BrokenProcessPool = BrokenExecutor

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .chart_context import ChartContextReader
from .embeddings import Embedder, VectorIndex, make_embedder
from .entity_linker import EntityLinker
from .evidence import build_reason_text, clean_boilerplate_text, extract_evidence
from .finance_filter import FastPrefilter
from .logging import get_logger
from .newsimpact_guarded_adapter import NewsImpactGuardedAdapter, NewsImpactGuardError
from .reranker import Reranker, make_reranker
from .schemas import (
    AwarenessCaptureView,
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from .scoring import ScoringInputs, estimate_entity_density, score
from .sentiment import SentimentClassifier, SentimentResult, make_sentiment
from .settings import CatchemMode, Settings
from .symbol_mapper import SymbolMapper
from .taxonomy import Taxonomy, default_taxonomy_path, load_taxonomy
from .zero_shot_classifier import ZeroShot, ZeroShotResult, make_zero_shot

logger = get_logger("catchem.service")


_MODE_MAP = {
    CatchemMode.PRODUCTION_SAFE: ProcessingMode.PRODUCTION_SAFE,
    CatchemMode.REPLAY_EXISTING: ProcessingMode.REPLAY_EXISTING,
    CatchemMode.LIVE_TAIL: ProcessingMode.LIVE_TAIL,
    CatchemMode.RESEARCH_DIAGNOSTIC: ProcessingMode.RESEARCH_DIAGNOSTIC,
}


# ── Process Isolation Worker State & Helpers ─────────────────────────────────

_worker_zero_shot = None
_worker_sentiment = None
_worker_embedder = None
_worker_reranker = None


def _init_worker(
    zero_shot_model_name: str,
    sentiment_model_name: str,
    embedding_model_name: str,
    reranker_model_name: str,
    use_ml_stubs: bool,
    taxonomy: Taxonomy,
    nice_value: int,
    memory_limit_mb: int | None,
) -> None:
    import os
    if hasattr(os, "nice"):
        try:
            os.nice(nice_value)
        except OSError:
            pass

    if memory_limit_mb is not None:
        try:
            import resource
            limit_bytes = memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except (OSError, ValueError, ImportError):
            pass

    global _worker_zero_shot, _worker_sentiment, _worker_embedder, _worker_reranker
    from .embeddings import make_embedder
    from .reranker import make_reranker
    from .sentiment import make_sentiment
    from .zero_shot_classifier import make_zero_shot

    _worker_zero_shot = make_zero_shot(taxonomy, zero_shot_model_name, use_ml_stubs)
    _worker_sentiment = make_sentiment(sentiment_model_name, use_ml_stubs)
    _worker_embedder = make_embedder(embedding_model_name, use_ml_stubs)
    _worker_reranker = make_reranker(reranker_model_name, use_ml_stubs)


def _worker_classify_zero_shot(cap: AwarenessCaptureView) -> ZeroShotResult:
    global _worker_zero_shot
    if _worker_zero_shot is None:
        raise RuntimeError("Worker zero_shot model not initialized")
    return _worker_zero_shot.classify(cap)


def _worker_classify_sentiment(cap: AwarenessCaptureView) -> SentimentResult:
    global _worker_sentiment
    if _worker_sentiment is None:
        raise RuntimeError("Worker sentiment model not initialized")
    return _worker_sentiment.classify(cap)


def _worker_encode_embedding(text: str) -> np.ndarray:
    global _worker_embedder
    if _worker_embedder is None:
        raise RuntimeError("Worker embedder model not initialized")
    return _worker_embedder.encode(text)


def _worker_encode_many_embedding(texts: list[str]) -> np.ndarray:
    global _worker_embedder
    if _worker_embedder is None:
        raise RuntimeError("Worker embedder model not initialized")
    return _worker_embedder.encode_many(texts)


def _worker_rank_reranker(query: str, candidates: list[str]) -> list[tuple[str, float]]:
    global _worker_reranker
    if _worker_reranker is None:
        raise RuntimeError("Worker reranker model not initialized")
    return _worker_reranker.rank(query, candidates)


# ── Thread-Safe FIFO Cache ──────────────────────────────────────────────────

class FIFOCache:
    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._cache: dict[Any, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            return self._cache.get(key)

    def set(self, key: Any, value: Any) -> None:
        if self._maxsize <= 0:
            return
        with self._lock:
            if key in self._cache:
                self._cache[key] = value
                return
            if len(self._cache) >= self._maxsize:
                first_key = next(iter(self._cache))
                self._cache.pop(first_key)
            self._cache[key] = value


# ── Resilient Executor Proxy ────────────────────────────────────────────────

class ResilientFuture:
    """Wrapper around Future that catches process/executor failures and retries once."""

    def __init__(
        self,
        executor: ResilientExecutor,
        pool: Executor,
        future: Any,
        fn: Any,
        args: Any,
        kwargs: Any,
    ) -> None:
        self._executor = executor
        self._pool = pool
        self._future = future
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def result(self, timeout: float | None = None) -> Any:
        try:
            return self._future.result(timeout=timeout)
        except (BrokenExecutor, BrokenProcessPool) as exc:
            logger.warning("Future.result raised BrokenExecutor, attempting recreation and retry.")
            self._executor.recreate_pool(self._pool)
            with self._executor._lock:
                if self._executor._is_closed or self._executor._pool is None:
                    raise RuntimeError("Executor is closed") from exc
                pool = self._executor._pool
            self._future = pool.submit(self._fn, *self._args, **self._kwargs)
            return self._future.result(timeout=timeout)


class ResilientExecutor:
    """Executor proxy that automatically recreates underlying pool on BrokenExecutor."""

    def __init__(
        self,
        use_stubs: bool,
        max_workers: int,
        zero_shot_model_name: str,
        sentiment_model_name: str,
        embedding_model_name: str,
        reranker_model_name: str,
        taxonomy: Taxonomy,
        nice_value: int,
        memory_limit_mb: int | None,
    ) -> None:
        self.use_stubs = use_stubs
        self.max_workers = max_workers
        self.zero_shot_model_name = zero_shot_model_name
        self.sentiment_model_name = sentiment_model_name
        self.embedding_model_name = embedding_model_name
        self.reranker_model_name = reranker_model_name
        self.taxonomy = taxonomy
        self.nice_value = nice_value
        self.memory_limit_mb = memory_limit_mb

        self._lock = threading.Lock()
        self._pool: Executor | None = None
        self._is_closed = False

        self.recreate_pool()

    def recreate_pool(self, failed_pool: Executor | None = None) -> None:
        with self._lock:
            if self._is_closed:
                return
            if failed_pool is not None and self._pool is not failed_pool:
                # Already recreated by another thread
                return

            if self._pool is not None:
                logger.warning("Worker pool broken or recreating. Shutting down old pool.")
                try:
                    self._pool.shutdown(wait=False)
                except Exception:
                    pass

            initargs = (
                self.zero_shot_model_name,
                self.sentiment_model_name,
                self.embedding_model_name,
                self.reranker_model_name,
                self.use_stubs,
                self.taxonomy,
                self.nice_value,
                self.memory_limit_mb,
            )
            if self.use_stubs:
                self._pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_workers,
                    initializer=_init_worker,
                    initargs=initargs,
                )
            else:
                self._pool = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self.max_workers,
                    initializer=_init_worker,
                    initargs=initargs,
                )

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> ResilientFuture:
        with self._lock:
            if self._is_closed or self._pool is None:
                raise RuntimeError("Executor is closed")
            pool = self._pool

        try:
            fut = pool.submit(fn, *args, **kwargs)
        except (BrokenExecutor, BrokenProcessPool) as exc:
            logger.warning("Failed to submit task due to broken executor. Recreating pool and retrying submit.")
            self.recreate_pool(pool)
            with self._lock:
                if self._is_closed or self._pool is None:
                    raise RuntimeError("Executor is closed") from exc
                pool = self._pool
            fut = pool.submit(fn, *args, **kwargs)

        return ResilientFuture(self, pool, fut, fn, args, kwargs)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            self._is_closed = True
            if self._pool is not None:
                self._pool.shutdown(wait=wait)
                self._pool = None


# ── Process Isolated Delegates ───────────────────────────────────────────────

class ProcessIsolatedZeroShot:
    def __init__(self, pool: Executor, taxonomy: Taxonomy, model_version: str, cache_maxsize: int = 256) -> None:
        self._pool = pool
        self._taxonomy = taxonomy
        self._model_version = model_version
        self._cache = FIFOCache(cache_maxsize)

    @property
    def model_version(self) -> str:
        return self._model_version

    def classify(self, cap: AwarenessCaptureView) -> ZeroShotResult:
        key = cap.capture_id
        val = self._cache.get(key)
        if val is not None:
            return val
        res = self._pool.submit(_worker_classify_zero_shot, cap).result()
        self._cache.set(key, res)
        return res

    def classify_batch(self, caps: list[AwarenessCaptureView]) -> list[ZeroShotResult]:
        futures = []
        results = [None] * len(caps)
        indices_to_compute = []

        for idx, cap in enumerate(caps):
            val = self._cache.get(cap.capture_id)
            if val is not None:
                results[idx] = val
            else:
                indices_to_compute.append(idx)
                futures.append(self._pool.submit(_worker_classify_zero_shot, cap))

        for idx, fut in zip(indices_to_compute, futures, strict=True):
            res = fut.result()
            results[idx] = res
            self._cache.set(caps[idx].capture_id, res)

        return [r for r in results if r is not None]


class ProcessIsolatedSentiment:
    def __init__(self, pool: Executor, model_version: str, cache_maxsize: int = 256) -> None:
        self._pool = pool
        self._model_version = model_version
        self._cache = FIFOCache(cache_maxsize)

    @property
    def model_version(self) -> str:
        return self._model_version

    def classify(self, cap: AwarenessCaptureView) -> SentimentResult:
        key = cap.capture_id
        val = self._cache.get(key)
        if val is not None:
            return val
        res = self._pool.submit(_worker_classify_sentiment, cap).result()
        self._cache.set(key, res)
        return res

    def classify_batch(self, caps: list[AwarenessCaptureView]) -> list[SentimentResult]:
        futures = []
        results = [None] * len(caps)
        indices_to_compute = []

        for idx, cap in enumerate(caps):
            val = self._cache.get(cap.capture_id)
            if val is not None:
                results[idx] = val
            else:
                indices_to_compute.append(idx)
                futures.append(self._pool.submit(_worker_classify_sentiment, cap))

        for idx, fut in zip(indices_to_compute, futures, strict=True):
            res = fut.result()
            results[idx] = res
            self._cache.set(caps[idx].capture_id, res)

        return [r for r in results if r is not None]


class ProcessIsolatedEmbedder:
    def __init__(self, pool: Executor, model_version: str, cache_maxsize: int = 256) -> None:
        self._pool = pool
        self._model_version = model_version
        self._cache = FIFOCache(cache_maxsize)

    @property
    def model_version(self) -> str:
        return self._model_version

    def encode(self, text: str) -> np.ndarray:
        val = self._cache.get(text)
        if val is not None:
            return val
        res = self._pool.submit(_worker_encode_embedding, text).result()
        self._cache.set(text, res)
        return res

    def encode_many(self, texts: Iterable[str]) -> np.ndarray:
        texts_list = list(texts)
        if not texts_list:
            dim = len(self.encode(""))
            return np.zeros((0, dim), dtype=np.float32)

        results = [None] * len(texts_list)
        missing_indices = []
        missing_texts = []

        for idx, text in enumerate(texts_list):
            cached_val = self._cache.get(text)
            if cached_val is not None:
                results[idx] = cached_val
            else:
                missing_indices.append(idx)
                missing_texts.append(text)

        if missing_texts:
            encoded_missing = self._pool.submit(_worker_encode_many_embedding, missing_texts).result()
            for idx, missing_idx in enumerate(missing_indices):
                val = encoded_missing[idx]
                results[missing_idx] = val
                self._cache.set(texts_list[missing_idx], val)

        return np.stack(results)


class ProcessIsolatedReranker:
    def __init__(self, pool: Executor, model_version: str, cache_maxsize: int = 256) -> None:
        self._pool = pool
        self._model_version = model_version
        self._cache = FIFOCache(cache_maxsize)

    @property
    def model_version(self) -> str:
        return self._model_version

    def rank(self, query: str, candidates: Iterable[str]) -> list[tuple[str, float]]:
        candidates_list = list(candidates)
        key = (query, tuple(candidates_list))
        val = self._cache.get(key)
        if val is not None:
            return val
        res = self._pool.submit(_worker_rank_reranker, query, candidates_list).result()
        self._cache.set(key, res)
        return res


class CatchemService:
    """Stateful pipeline. Construct once per process; ``process`` per capture."""

    def __init__(
        self,
        settings: Settings,
        taxonomy: Taxonomy,
        vector_index: VectorIndex | None = None,
    ) -> None:
        self.settings = settings
        self.taxonomy = taxonomy

        use_stubs = bool(settings.models.use_ml_stubs)
        self.prefilter = FastPrefilter(taxonomy=taxonomy)

        if settings.models.isolation_enabled is not None:
            self.use_isolation = settings.models.isolation_enabled
        else:
            self.use_isolation = not use_stubs

        zero_shot_ver = "stub-zero-shot/v1" if use_stubs else f"hf:{settings.models.zero_shot}"
        sentiment_ver = "stub-sentiment/v1" if use_stubs else f"hf:{settings.models.sentiment_default}"
        embedder_ver = "stub-embedding/v1" if use_stubs else f"hf:{settings.models.embedding}"
        reranker_ver = "stub-reranker/v1" if use_stubs else f"hf:{settings.models.reranker}"

        if self.use_isolation:
            self._pool = ResilientExecutor(
                use_stubs=use_stubs,
                max_workers=settings.models.isolation_processes,
                zero_shot_model_name=settings.models.zero_shot,
                sentiment_model_name=settings.models.sentiment_default,
                embedding_model_name=settings.models.embedding,
                reranker_model_name=settings.models.reranker,
                taxonomy=taxonomy,
                nice_value=settings.models.isolation_nice,
                memory_limit_mb=settings.models.isolation_memory_limit_mb,
            )
            self.zero_shot: ZeroShot = ProcessIsolatedZeroShot(
                self._pool, taxonomy, zero_shot_ver, settings.models.cache_maxsize
            )
            self.sentiment: SentimentClassifier = ProcessIsolatedSentiment(
                self._pool, sentiment_ver, settings.models.cache_maxsize
            )
            self.embedder: Embedder = ProcessIsolatedEmbedder(
                self._pool, embedder_ver, settings.models.cache_maxsize
            )
            self.reranker: Reranker = ProcessIsolatedReranker(
                self._pool, reranker_ver, settings.models.cache_maxsize
            )
        else:
            self._pool = None
            self.zero_shot = make_zero_shot(taxonomy, settings.models.zero_shot, use_stubs)
            self.sentiment = make_sentiment(settings.models.sentiment_default, use_stubs)
            self.embedder = make_embedder(settings.models.embedding, use_stubs)
            self.reranker = make_reranker(settings.models.reranker, use_stubs)

        config_path = Path(__file__).resolve().parents[2] / "configs" / "symbols.yaml"
        self.symbol_mapper = SymbolMapper(
            config_path=config_path if config_path.exists() else None,
            newsimpact_root=settings.paths.newsimpact_repo
        )
        self.entity_linker = EntityLinker(company_aliases=self.symbol_mapper.alias_dict())
        self.chart_reader = ChartContextReader(settings.paths.newsimpact_repo)
        self.vector_index = vector_index

        # Diagnostic adapter is constructed lazily — only if both mode and flag agree.
        self._diagnostic_adapter: NewsImpactGuardedAdapter | None = None
        if settings.diagnostic_allowed():
            try:
                self._diagnostic_adapter = NewsImpactGuardedAdapter(
                    newsimpact_root=settings.paths.newsimpact_repo,
                    mode=settings.mode.value,
                    diagnostic_flag=settings.guards.newsimpact_diagnostic_enabled,
                    allow_modes=settings.guards.allow_research_diagnostic_in_modes,
                )
            except NewsImpactGuardError as exc:
                logger.warning("diagnostic_adapter_refused", reason=str(exc))
                self._diagnostic_adapter = None

    def close(self) -> None:
        if hasattr(self, "_pool") and self._pool is not None:
            try:
                self._pool.shutdown(wait=True)
            except Exception:
                pass
            self._pool = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> CatchemService:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @property
    def diagnostic_enabled(self) -> bool:
        return self._diagnostic_adapter is not None

    @property
    def model_versions(self) -> Mapping[str, str]:
        return {
            "zero_shot": self.zero_shot.model_version,
            "sentiment": self.sentiment.model_version,
            "embedding": self.embedder.model_version,
            "reranker": self.reranker.model_version,
            "prefilter": "rule:v1",
            "scoring": "rule:v1",
        }

    # ── batch-capture pipeline ──────────────────────────────────────────────
    def process_batch(self, caps: list[AwarenessCaptureView]) -> list[FinancialImpactRecord]:
        if not caps:
            return []

        try:
            is_patched = (self.process.__func__ is not CatchemService.process)
        except AttributeError:
            is_patched = True

        if is_patched:
            return [self.process(cap) for cap in caps]

        # Step 1: Evaluate prefilters
        pres = [self.prefilter.evaluate(cap) for cap in caps]

        # Step 2: Zero Shot Classifications (batched if supported)
        if hasattr(self.zero_shot, "classify_batch"):
            zs_results = self.zero_shot.classify_batch(caps)
        else:
            zs_results = [self.zero_shot.classify(cap) for cap in caps]

        # Step 3: Sentiment Classifications (batched if supported)
        if hasattr(self.sentiment, "classify_batch"):
            sent_results = self.sentiment.classify_batch(caps)
        else:
            sent_results = [self.sentiment.classify(cap) for cap in caps]

        # Step 4: Vector Embeddings (batched if supported)
        if self.vector_index is not None:
            try:
                embed_texts = [(cap.title or "") + "\n" + (cap.text or "")[:1500] for cap in caps]
                if hasattr(self.embedder, "encode_many"):
                    vecs = self.embedder.encode_many(embed_texts)
                else:
                    vecs = [self.embedder.encode(t) for t in embed_texts]
                for cap, vec in zip(caps, vecs, strict=True):
                    self.vector_index.save(cap.capture_id, vec)
            except Exception as exc:
                logger.warning("embedding_save_failed", err=str(exc))

        # Step 5: Element-wise pipeline for the remaining non-heavy components
        records = []
        for cap, pre, zs, sent in zip(caps, pres, zs_results, sent_results, strict=True):
            clean_text = clean_boilerplate_text(cap.text or "")
            ents = self.entity_linker.extract(cap.title, clean_text)

            # Symbol mapping over title (title is more discriminative)
            symbol_matches = self.symbol_mapper.map_text((cap.title or "") + "\n" + clean_text[:2000])
            candidate_symbols = [m.symbol for m in symbol_matches]
            candidate_entities = ents.unique_texts()

            # Light reranking only when there are >1 symbol candidates
            if len(candidate_symbols) > 1:
                ranked = self.reranker.rank(cap.title or "", candidate_symbols)
                candidate_symbols = [c for c, _ in ranked]

            ac_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.asset_class_ids}
            rc_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.reason_code_ids}
            neg_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.negative_class_ids}

            if "equities" in self.taxonomy.asset_class_ids:
                has_equity_hit = any(h.kind == "cashtag" for h in ents.hits) or any(
                    h.kind == "ticker" and _looks_like_equity_ticker(h.text)
                    for h in ents.hits
                )
                if has_equity_hit:
                    ac_scores["equities"] = max(ac_scores.get("equities", 0.0), 0.6)

            finance_hit_kinds = {"cashtag", "ticker", "currency", "central_bank", "index", "commodity", "crypto"}
            finance_hits = sum(1 for h in ents.hits if h.kind in finance_hit_kinds)
            density = estimate_entity_density(num_hits=finance_hits, text_length=len(cap.text or ""))
            non_neutral = sent.score if sent.label in (SentimentLabel.POSITIVE, SentimentLabel.NEGATIVE) else 0.0

            scoring_outputs = score(
                ScoringInputs(
                    prefilter_rule_score=pre.rule_score,
                    domain_prior=pre.domain_prior,
                    source_type_prior=pre.source_type_prior,
                    asset_class_scores=ac_scores,
                    reason_code_scores=rc_scores,
                    negative_class_scores=neg_scores,
                    sentiment_confidence=non_neutral,
                    entity_density=density,
                ),
                taxonomy=self.taxonomy,
            )

            # Horizons
            impact_horizons = _horizons_from_reasons(scoring_outputs.reason_codes_passed)

            # Evidence
            label_terms = (
                list(scoring_outputs.asset_classes_passed)
                + list(scoring_outputs.reason_codes_passed)
            )
            entity_terms = candidate_entities[:8]
            evidence = extract_evidence(cap, label_terms, entity_terms, top_k=self.taxonomy.threshold("evidence_top_k", 3))

            reason_text = build_reason_text(
                scoring_outputs.asset_classes_passed,
                scoring_outputs.reason_codes_passed,
                sent.label.value if sent.label != SentimentLabel.UNKNOWN else None,
            )

            # Diagnostic (research mode only)
            diag_payload = None
            if self._diagnostic_adapter is not None:
                diag_payload = self._diagnostic_adapter.diagnostic_payload(
                    capture_id=cap.capture_id, text=cap.text
                )

            # Component scores from zero-shot too (keep top-3 per group for transparency)
            comp = dict(scoring_outputs.component_scores)
            for k, v in sorted(ac_scores.items(), key=lambda kv: -kv[1])[:3]:
                comp[f"ac_{k}"] = float(v)
            for k, v in sorted(rc_scores.items(), key=lambda kv: -kv[1])[:3]:
                comp[f"rc_{k}"] = float(v)
            if neg_scores:
                comp["neg_max"] = float(max(neg_scores.values()))

            text_excerpt = (cap.text or "")[: self.settings.replay.text_excerpt_chars] or (cap.title or "(no body)")

            records.append(
                FinancialImpactRecord(
                    capture_id=cap.capture_id,
                    doc_id=cap.doc_id,
                    title=cap.title,
                    text_excerpt=text_excerpt,
                    published_ts=cap.published_ts,
                    domain=cap.domain,
                    language=cap.language,
                    url=cap.url,
                    is_finance_relevant=bool(scoring_outputs.is_finance_relevant and pre.keep),
                    finance_relevance_score=float(scoring_outputs.finance_relevance_score),
                    asset_classes=list(scoring_outputs.asset_classes_passed),
                    impact_reason_codes=list(scoring_outputs.reason_codes_passed),
                    candidate_symbols=candidate_symbols[:8],
                    candidate_entities=candidate_entities[:12],
                    impact_horizons=impact_horizons,
                    sentiment_label=sent.label,
                    sentiment_score=float(sent.score),
                    evidence_sentences=evidence,
                    reason_text=reason_text,
                    component_scores=comp,
                    diagnostic_multimodal_enabled=self.diagnostic_enabled,
                    diagnostic_multimodal_result=diag_payload,
                    processing_mode=_MODE_MAP[self.settings.mode],
                    model_versions=dict(self.model_versions),
                    created_at=datetime.now(UTC),
                )
            )

        return records

    # ── single-capture pipeline ─────────────────────────────────────────────
    def process(self, cap: AwarenessCaptureView) -> FinancialImpactRecord:
        # Stage A
        pre = self.prefilter.evaluate(cap)

        # Short-circuit: clear-non-finance items still get a record (with is_finance_relevant=False),
        # so the dashboard can show what was filtered out.
        zs = self.zero_shot.classify(cap)
        sent = self.sentiment.classify(cap)
        clean_text = clean_boilerplate_text(cap.text or "")
        ents = self.entity_linker.extract(cap.title, clean_text)

        # Symbol mapping over title (title is more discriminative)
        symbol_matches = self.symbol_mapper.map_text((cap.title or "") + "\n" + clean_text[:2000])
        candidate_symbols = [m.symbol for m in symbol_matches]
        candidate_entities = ents.unique_texts()

        # Light reranking only when there are >1 symbol candidates
        if len(candidate_symbols) > 1:
            ranked = self.reranker.rank(cap.title or "", candidate_symbols)
            candidate_symbols = [c for c, _ in ranked]

        ac_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.asset_class_ids}
        rc_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.reason_code_ids}
        neg_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.negative_class_ids}

        # BUG-BB: cashtag/ticker hits are a high-confidence "this is about a
        # specific tradeable equity" signal. Pre-fix the zero-shot stub
        # picked up "equities" only when the taxonomy alias set
        # (`stocks`/`shares`/`equity`) happened to appear in the text — a
        # press release saying `$AAPL rose 4% in after-hours trading on the
        # news` carried the ticker but none of the aliases, so `asset=[]`
        # came out of an obviously equities-relevant story. Bridge symbol
        # detection back into the asset-class layer so the channel mapping
        # and downstream consumers see equities.
        #
        # BUG-BB.1: the first cut treated ANY ticker hit as equity, but
        # EntityLinker also resolves crypto/FX/commodity/index aliases into
        # the ticker kind (Bitcoin → BTC-USD, EUR/USD → EURUSD=X, gold →
        # GC=F, S&P 500 → ^GSPC). A BTC-only headline then showed up with
        # asset_classes=['crypto', 'equities'] — a false equity tag. The
        # ticker format unambiguously encodes the asset class:
        #   - equity:   AAPL, MSFT, BRK.B  (plain uppercase, optional .X)
        #   - crypto:   *-USD              (suffix)
        #   - fx:       *=X                (suffix)
        #   - commodity:*=F                (suffix)
        #   - index:    ^*                 (prefix)
        # Cashtags ($TICKER) are unambiguous — $ is the equity convention.
        if "equities" in self.taxonomy.asset_class_ids:
            has_equity_hit = any(h.kind == "cashtag" for h in ents.hits) or any(
                h.kind == "ticker" and _looks_like_equity_ticker(h.text)
                for h in ents.hits
            )
            if has_equity_hit:
                ac_scores["equities"] = max(ac_scores.get("equities", 0.0), 0.6)

        # entity_density should only count finance-grounded hit kinds — generic
        # proper-noun runs ("Nazi", "Dutch SS leader") must not inflate it.
        finance_hit_kinds = {"cashtag", "ticker", "currency", "central_bank", "index", "commodity", "crypto"}
        finance_hits = sum(1 for h in ents.hits if h.kind in finance_hit_kinds)
        density = estimate_entity_density(num_hits=finance_hits, text_length=len(cap.text or ""))
        # Sentiment "non-neutralness": helpful for non-neutral signals.
        non_neutral = sent.score if sent.label in (SentimentLabel.POSITIVE, SentimentLabel.NEGATIVE) else 0.0

        scoring_outputs = score(
            ScoringInputs(
                prefilter_rule_score=pre.rule_score,
                domain_prior=pre.domain_prior,
                source_type_prior=pre.source_type_prior,
                asset_class_scores=ac_scores,
                reason_code_scores=rc_scores,
                negative_class_scores=neg_scores,
                sentiment_confidence=non_neutral,
                entity_density=density,
            ),
            taxonomy=self.taxonomy,
        )

        # Horizons: simple heuristic from reason codes.
        impact_horizons = _horizons_from_reasons(scoring_outputs.reason_codes_passed)

        # Evidence
        label_terms = (
            list(scoring_outputs.asset_classes_passed)
            + list(scoring_outputs.reason_codes_passed)
        )
        entity_terms = candidate_entities[:8]
        evidence = extract_evidence(cap, label_terms, entity_terms, top_k=self.taxonomy.threshold("evidence_top_k", 3))

        reason_text = build_reason_text(
            scoring_outputs.asset_classes_passed,
            scoring_outputs.reason_codes_passed,
            sent.label.value if sent.label != SentimentLabel.UNKNOWN else None,
        )

        # Optionally store embedding
        if self.vector_index is not None:
            try:
                vec = self.embedder.encode((cap.title or "") + "\n" + (cap.text or "")[:1500])
                self.vector_index.save(cap.capture_id, vec)
            except Exception as exc:
                logger.warning("embedding_save_failed", err=str(exc))

        # Diagnostic (research mode only)
        diag_payload = None
        if self._diagnostic_adapter is not None:
            diag_payload = self._diagnostic_adapter.diagnostic_payload(
                capture_id=cap.capture_id, text=cap.text
            )

        # Component scores from zero-shot too (keep top-3 per group for transparency)
        comp = dict(scoring_outputs.component_scores)
        for k, v in sorted(ac_scores.items(), key=lambda kv: -kv[1])[:3]:
            comp[f"ac_{k}"] = float(v)
        for k, v in sorted(rc_scores.items(), key=lambda kv: -kv[1])[:3]:
            comp[f"rc_{k}"] = float(v)
        if neg_scores:
            comp["neg_max"] = float(max(neg_scores.values()))

        text_excerpt = (cap.text or "")[: self.settings.replay.text_excerpt_chars] or (cap.title or "(no body)")

        return FinancialImpactRecord(
            capture_id=cap.capture_id,
            doc_id=cap.doc_id,
            title=cap.title,
            text_excerpt=text_excerpt,
            published_ts=cap.published_ts,
            domain=cap.domain,
            language=cap.language,
            url=cap.url,
            is_finance_relevant=bool(scoring_outputs.is_finance_relevant and pre.keep),
            finance_relevance_score=float(scoring_outputs.finance_relevance_score),
            asset_classes=list(scoring_outputs.asset_classes_passed),
            impact_reason_codes=list(scoring_outputs.reason_codes_passed),
            candidate_symbols=candidate_symbols[:8],
            candidate_entities=candidate_entities[:12],
            impact_horizons=impact_horizons,
            sentiment_label=sent.label,
            sentiment_score=float(sent.score),
            evidence_sentences=evidence,
            reason_text=reason_text,
            component_scores=comp,
            diagnostic_multimodal_enabled=self.diagnostic_enabled,
            diagnostic_multimodal_result=diag_payload,
            processing_mode=_MODE_MAP[self.settings.mode],
            model_versions=dict(self.model_versions),
            created_at=datetime.now(UTC),
        )


def _looks_like_equity_ticker(text: str) -> bool:
    """Return True when `text` matches the bare-uppercase equity ticker
    convention (AAPL, MSFT, BRK.B, GOOGL). Rejects crypto (`-USD`),
    FX (`=X`), commodity (`=F`), and index (`^TICKER`) formats — see
    `_INTERNAL_REGISTRY` in symbol_mapper for the canonical examples of
    each asset class.

    Equity tickers are 1-8 chars, uppercase, optionally with a dot-suffix
    for share classes. Empty / lowercase / suffixed / prefixed → False.
    """
    if not text or len(text) > 8:
        return False
    if text.startswith("^") or "=" in text or "-" in text:
        return False
    return all(c.isupper() or c == "." for c in text)


def _horizons_from_reasons(reasons: tuple[str, ...]) -> list[str]:
    if not reasons:
        return []
    out: set[str] = set()
    # BUG-AA: `product_launch` was the only reason code in the taxonomy
    # (28 total) silently dropped by this mapping — product-launch records
    # surfaced with `impact_horizons=[]`. Launches typically move the stock
    # intraday/one_day on the announcement.
    short_term = {"central_bank", "earnings", "guidance", "cyber_outage", "natural_disaster",
                  "fraud_governance", "litigation", "product_launch"}
    one_week = {"m_and_a", "regulation", "sanctions_trade", "supply_chain", "energy", "metals"}
    structural = {"inflation", "growth_recession", "employment", "esg_reputation", "funding_liquidity",
                  "geopolitics"}
    for r in reasons:
        if r in short_term:
            out.update({"intraday", "one_day"})
        if r in one_week:
            out.add("one_week")
        if r in structural:
            out.add("structural")
    return sorted(out)


def _horizon_buckets() -> tuple[set[str], set[str], set[str]]:
    """Exposed for test access. Mirrors the buckets in `_horizons_from_reasons`."""
    short_term = {"central_bank", "earnings", "guidance", "cyber_outage", "natural_disaster",
                  "fraud_governance", "litigation", "product_launch"}
    one_week = {"m_and_a", "regulation", "sanctions_trade", "supply_chain", "energy", "metals"}
    structural = {"inflation", "growth_recession", "employment", "esg_reputation", "funding_liquidity",
                  "geopolitics"}
    return short_term, one_week, structural


def build_service(settings: Settings, vector_index: VectorIndex | None = None) -> CatchemService:
    taxonomy = load_taxonomy(default_taxonomy_path())
    return CatchemService(settings=settings, taxonomy=taxonomy, vector_index=vector_index)
