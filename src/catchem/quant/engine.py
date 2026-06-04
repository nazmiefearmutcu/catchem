"""QuantEngine — facade over the six signal modules.

The supervisor + API + UI talk only to this class. Each method is a thin
projection over the underlying signal module; the engine owns:
  * lazy storage reads (records pulled once per request, reused)
  * caching of expensive computations (TTL-based, in-memory)
  * a uniform error envelope (fail-soft per module — one broken signal
    must NOT block the others)
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import is_dataclass
from typing import Any

from ..logging import get_logger
from ..market_data import LocalFixtureMarketDataProvider, MarketQuote
from .anomaly import AnomalyReport, detect_anomalies
from .co_occurrence import CoOccurrenceReport, compute_co_occurrence
from .event_clustering import EventCluster, cluster_records
from .lead_lag import LeadLagReport, attribute_lead_lag
from .market_reaction import ReactionReport, compute_reaction
from .novelty import NoveltyResult, compute_novelty, score_corpus
from .sentiment_momentum import SentimentMomentumReport, compute_sentiment_momentum
from .source_reliability import SourceLeaderboard, compute_source_scores
from .spillover import SpilloverReport, compute_spillover
from .topic_regime import RegimeReport, detect_regime_shifts

logger = get_logger("catchem.quant.engine")


class _QuoteAdapter:
    """Adapts the existing `LocalFixtureMarketDataProvider.quote(symbol)`
    to the `.get_quote(symbol)` shape that `compute_reaction` expects.

    Agent 2's deliverable accepts any object with `.get_quote()`; the
    real provider's method is named `quote()`. This shim bridges that
    without modifying either side.
    """

    def __init__(self, provider) -> None:
        self._provider = provider

    def get_quote(self, symbol: str) -> MarketQuote:
        return self._provider.quote(symbol)


# ── observability ring buffer ────────────────────────────────────────────
# _safe_call previously swallowed every exception with a single
# `logger.warning(...)` call and returned None. That kept the dashboard
# alive (correct behavior — one broken signal must NOT block the others)
# but made the failure invisible at runtime: there was no way for an
# operator to see, from the UI, which signals had degraded and why.
#
# The buffer below is a per-process, thread-safe deque of the last 50
# failures. Each entry captures the signal label, error class, message,
# truncated traceback head, wall-clock seconds spent before failure,
# and an ISO-ish timestamp. `_diagnostics_snapshot()` reads it for the
# /api/quant/diagnostics endpoint.
_SIGNAL_FAILURES: deque[dict[str, Any]] = deque(maxlen=50)
_SIGNAL_FAILURES_LOCK = threading.Lock()


def _record_failure(label: str, exc: BaseException, elapsed_ms: float) -> None:
    """Append a single failure record to the ring buffer."""
    # `traceback.format_exception` returns a list of strings — concatenate
    # then trim to ~600 chars to keep the buffer bounded. Full traceback is
    # always available in the structured log line for deeper diagnostics.
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = {
        "signal": label,
        "error_class": type(exc).__name__,
        "error": str(exc),
        "traceback_head": tb_text[-600:],  # last frames are most informative
        "elapsed_ms": round(elapsed_ms, 2),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _SIGNAL_FAILURES_LOCK:
        _SIGNAL_FAILURES.append(entry)


def _diagnostics_snapshot() -> list[dict[str, Any]]:
    """Public-ish read of the failure ring (newest last)."""
    with _SIGNAL_FAILURES_LOCK:
        return list(_SIGNAL_FAILURES)


def _diagnostics_clear() -> None:
    """Test-only reset of the failure ring."""
    with _SIGNAL_FAILURES_LOCK:
        _SIGNAL_FAILURES.clear()


def _safe_call(fn: Callable[..., Any], *args, label: str, **kwargs) -> Any:
    """Run a signal, log + return None on failure. Fail-soft per module.

    On exception, the failure is also captured into the in-process ring
    buffer (`_SIGNAL_FAILURES`) so `/api/quant/diagnostics` can surface
    the last-N failures with class + message + traceback head — silent
    fail-soft degradation is now observable, not invisible.
    """
    started = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # pragma: no cover — exercised in integration
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.warning(
            "quant_signal_failed",
            signal=label,
            error_class=type(exc).__name__,
            error=str(exc),
            elapsed_ms=round(elapsed_ms, 2),
        )
        _record_failure(label, exc, elapsed_ms)
        return None


def _dataclass_to_dict(value: Any) -> Any:
    """Recursively serialize dataclasses for JSON output."""
    t = type(value)
    if t in (str, int, float, bool, type(None)):
        return value
    if t is list:
        return [_dataclass_to_dict(v) for v in value]
    if t is dict:
        return {k: _dataclass_to_dict(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f: _dataclass_to_dict(getattr(value, f)) for f in value.__dataclass_fields__}
    if t is tuple:
        return [_dataclass_to_dict(v) for v in value]
    return value


class QuantEngine:
    """One-stop access to the six quant signals."""

    def __init__(
        self,
        *,
        storage,
        market_provider=None,
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        self._storage = storage
        self._market = _QuoteAdapter(market_provider or LocalFixtureMarketDataProvider())
        self._ttl = float(cache_ttl_seconds)
        self._cache: dict[str, tuple[float, Any]] = {}
        # dashboard_snapshot() fans the signals out across a ThreadPoolExecutor,
        # so multiple worker threads hit _cached() concurrently. A plain dict is
        # not safe under concurrent __setitem__ (a rehash mid-insert can drop or
        # duplicate entries), so guard every cache read/write with this lock.
        self._cache_lock = threading.Lock()
        self._pending_events: dict[str, threading.Event] = {}

    # ── cache helpers ────────────────────────────────────────────────────
    def _cached(self, key: str, build: Callable[[], Any]) -> Any:
        now = time.time()
        # 1. Fast-path: lock-free read of the cache
        hit = self._cache.get(key)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]

        event_to_wait = None
        my_event = None

        with self._cache_lock:
            # Double-check under lock
            hit = self._cache.get(key)
            if hit is not None and (now - hit[0]) < self._ttl:
                return hit[1]

            # Check if another thread is currently building this key
            if key in self._pending_events:
                event_to_wait = self._pending_events[key]
            else:
                # We are the builder for this key
                my_event = threading.Event()
                self._pending_events[key] = my_event

        if event_to_wait is not None:
            event_to_wait.wait()
            # Retrieve value after the other thread completes building it
            hit = self._cache.get(key)
            if hit is not None:
                return hit[1]
            return build()

        success = False
        try:
            value = build()
            success = True
        finally:
            with self._cache_lock:
                if success:
                    self._cache[key] = (time.time(), value)
                self._pending_events.pop(key, None)
                my_event.set()

        return value

    def invalidate(self) -> None:
        """Drop all cached signals.

        Called by the manual ``POST /api/quant/invalidate`` endpoint and the
        ``/ui/news-poll-now`` (manual "Poll now") path. The background RSS
        poller does not call this — after an automatic poll the signals
        refresh via the ``cache_ttl_seconds`` (30s) expiry rather than an
        event-driven hook.
        """
        with self._cache_lock:
            self._cache.clear()

    # ── observability ───────────────────────────────────────────────────
    def diagnostics(self) -> dict[str, Any]:
        """Snapshot of recent signal failures + per-signal failure counts.

        Feeds /api/quant/diagnostics. The UI uses this to show a small
        "N signals degraded" pill on the QuantScan hero so the operator
        knows when a previously green dashboard is actually masking a
        crash in (say) the spillover module.
        """
        failures = _diagnostics_snapshot()
        # Aggregate counts so the UI can render `{signal: count}` without
        # iterating the raw failure list. Newest-first is more intuitive
        # for the operator scanning recent activity.
        counts: dict[str, int] = {}
        for f in failures:
            counts[f["signal"]] = counts.get(f["signal"], 0) + 1
        return {
            "total_failures": len(failures),
            "per_signal": counts,
            "recent": list(reversed(failures)),  # newest first
            "buffer_capacity": _SIGNAL_FAILURES.maxlen,
        }

    # ── record provider ──────────────────────────────────────────────────
    def _recent_records(self, limit: int = 500, relevant_only: bool = False) -> list[dict]:
        """Pull the recent corpus once per request and reuse across signals."""
        cache_key = f"records:{limit}:{relevant_only}"
        return self._cached(
            cache_key, lambda: self._storage.recent_records(limit=limit, relevant_only=relevant_only)
        )

    # ── signals ──────────────────────────────────────────────────────────
    def clusters(
        self,
        *,
        limit: int = 500,
        window_seconds: int = 1800,
        similarity_threshold: float = 0.35,
        min_cluster_size: int = 2,
        min_distinct_domains: int = 2,
    ) -> list[EventCluster]:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"clusters:{limit}:{window_seconds}:{similarity_threshold}:{min_cluster_size}:{min_distinct_domains}"
        return self._cached(
            key,
            lambda: (
                _safe_call(
                    cluster_records,
                    records,
                    window_seconds=window_seconds,
                    similarity_threshold=similarity_threshold,
                    min_cluster_size=min_cluster_size,
                    min_distinct_domains=min_distinct_domains,
                    label="event_clustering",
                )
                or []
            ),
        )

    def source_leaderboard(
        self,
        *,
        limit: int = 1000,
        window_days: int = 30,
        min_records: int = 3,
    ) -> SourceLeaderboard | None:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"sources:{limit}:{window_days}:{min_records}"
        return self._cached(
            key,
            lambda: _safe_call(
                compute_source_scores,
                records,
                window_days=window_days,
                min_records=min_records,
                label="source_reliability",
            ),
        )

    def novelty_for(self, capture_id: str, *, corpus_limit: int = 500) -> NoveltyResult | None:
        record = self._storage.get_record(capture_id)
        if record is None:
            return None
        corpus = self._recent_records(limit=corpus_limit, relevant_only=False)
        key = f"novelty:{capture_id}:{corpus_limit}"
        return self._cached(
            key,
            lambda: _safe_call(compute_novelty, record, corpus, label="novelty"),
        )

    def novelty_timeline(self, *, limit: int = 200) -> list[NoveltyResult]:
        corpus = self._recent_records(limit=limit, relevant_only=False)
        key = f"novelty-timeline:{limit}"
        return self._cached(
            key,
            lambda: _safe_call(score_corpus, corpus, label="novelty_timeline") or [],
        )

    def lead_lag(self, *, limit: int = 500) -> LeadLagReport | None:
        clusters = self.clusters(limit=limit)
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"leadlag:{limit}"
        return self._cached(
            key,
            lambda: _safe_call(attribute_lead_lag, clusters, records, label="lead_lag"),
        )

    def regime(
        self,
        *,
        limit: int = 1000,
        bucket_minutes: int = 60,
        shift_threshold: float = 0.40,
        min_records_per_bucket: int = 3,
    ) -> RegimeReport | None:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"regime:{limit}:{bucket_minutes}:{shift_threshold}:{min_records_per_bucket}"
        return self._cached(
            key,
            lambda: _safe_call(
                detect_regime_shifts,
                records,
                bucket_minutes=bucket_minutes,
                shift_threshold=shift_threshold,
                min_records_per_bucket=min_records_per_bucket,
                label="topic_regime",
            ),
        )

    def sentiment_momentum(
        self,
        *,
        limit: int = 1000,
        bucket_minutes: int = 240,
        min_mentions: int = 4,
        max_tickers: int = 25,
    ) -> SentimentMomentumReport | None:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"sent-momentum:{limit}:{bucket_minutes}:{min_mentions}:{max_tickers}"
        return self._cached(
            key,
            lambda: _safe_call(
                compute_sentiment_momentum,
                records,
                bucket_minutes=bucket_minutes,
                min_mentions=min_mentions,
                max_tickers=max_tickers,
                label="sentiment_momentum",
            ),
        )

    def co_occurrence(
        self,
        *,
        limit: int = 1000,
        min_pair_count: int = 1,
        min_edge_weight: int = 2,
        top_n_cells: int = 50,
        top_n_edges: int = 60,
    ) -> CoOccurrenceReport | None:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"cooc:{limit}:{min_pair_count}:{min_edge_weight}:{top_n_cells}:{top_n_edges}"
        return self._cached(
            key,
            lambda: _safe_call(
                compute_co_occurrence,
                records,
                min_pair_count=min_pair_count,
                min_edge_weight=min_edge_weight,
                top_n_cells=top_n_cells,
                top_n_edges=top_n_edges,
                label="co_occurrence",
            ),
        )

    def anomalies(
        self,
        *,
        limit: int = 1500,
        bucket_minutes: int = 30,
        window_buckets: int = 12,
        z_threshold: float = 2.0,
        top_n_symbols: int = 25,
    ) -> AnomalyReport | None:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"anomaly:{limit}:{bucket_minutes}:{window_buckets}:{z_threshold}:{top_n_symbols}"
        return self._cached(
            key,
            lambda: _safe_call(
                detect_anomalies,
                records,
                bucket_minutes=bucket_minutes,
                window_buckets=window_buckets,
                z_threshold=z_threshold,
                top_n_symbols=top_n_symbols,
                label="anomaly",
            ),
        )

    def spillover(
        self,
        *,
        limit: int = 1500,
        bucket_minutes: int = 30,
        lag_buckets: int = 1,
        surge_z_threshold: float = 1.5,
    ) -> SpilloverReport | None:
        records = self._recent_records(limit=limit, relevant_only=False)
        key = f"spillover:{limit}:{bucket_minutes}:{lag_buckets}:{surge_z_threshold}"
        return self._cached(
            key,
            lambda: _safe_call(
                compute_spillover,
                records,
                bucket_minutes=bucket_minutes,
                lag_buckets=lag_buckets,
                surge_z_threshold=surge_z_threshold,
                label="spillover",
            ),
        )

    def reaction_for(self, capture_id: str) -> ReactionReport | None:
        record = self._storage.get_record(capture_id)
        if record is None:
            return None
        key = f"reaction:{capture_id}"
        return self._cached(
            key,
            lambda: _safe_call(compute_reaction, record, self._market, label="market_reaction"),
        )

    # ── summary for the /scan dashboard ──────────────────────────────────
    def dashboard_snapshot(self, *, limit: int = 500) -> dict[str, Any]:
        """One-call payload feeding the /scan UI cockpit. All 10 signals.

        Signals run in parallel via a ThreadPoolExecutor — they're all
        CPU-bound pure-Python over the same already-cached corpus, so
        the GIL forces them to round-robin, but I/O-side warmup
        (SQLite read for the corpus, JSON-serialize at the end) overlaps
        cleanly. Empirically the fan-out drops a cold dashboard call
        from ~1.5s to ~0.5s on a 1000-record window. Cache hits skip
        the executor entirely.
        """
        from concurrent.futures import ThreadPoolExecutor

        # Prime the corpus cache so every worker reads from memory, and keep
        # the actual row count for the dashboard contract. This is not always
        # equal to the requested limit on a fresh/empty workstation.
        records = self._recent_records(limit=limit, relevant_only=False)

        signals: dict[str, Any] = {}
        novelty_limit = min(200, limit)

        def _do(name: str, fn):
            signals[name] = fn()

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="quant-signal") as pool:
            pool.submit(_do, "clusters", lambda: self.clusters(limit=limit))
            pool.submit(_do, "leaderboard", lambda: self.source_leaderboard(limit=limit))
            pool.submit(_do, "novelty", lambda: self.novelty_timeline(limit=novelty_limit))
            pool.submit(_do, "lead_lag", lambda: self.lead_lag(limit=limit))
            pool.submit(_do, "regime", lambda: self.regime(limit=limit))
            pool.submit(_do, "sent_momentum", lambda: self.sentiment_momentum(limit=limit))
            pool.submit(_do, "cooc", lambda: self.co_occurrence(limit=limit))
            pool.submit(_do, "anomaly", lambda: self.anomalies(limit=limit))
            pool.submit(_do, "spillover", lambda: self.spillover(limit=limit))

        clusters = signals.get("clusters") or []
        return {
            "n_records_window": len(records),
            "n_clusters": len(clusters),
            "clusters": [_dataclass_to_dict(c) for c in clusters[:50]],
            "source_leaderboard": _dataclass_to_dict(signals.get("leaderboard"))
            if signals.get("leaderboard")
            else None,
            "novelty_timeline": [_dataclass_to_dict(n) for n in (signals.get("novelty") or [])[:100]],
            "lead_lag": _dataclass_to_dict(signals.get("lead_lag")) if signals.get("lead_lag") else None,
            "regime": _dataclass_to_dict(signals.get("regime")) if signals.get("regime") else None,
            "sentiment_momentum": _dataclass_to_dict(signals.get("sent_momentum"))
            if signals.get("sent_momentum")
            else None,
            "co_occurrence": _dataclass_to_dict(signals.get("cooc")) if signals.get("cooc") else None,
            "anomalies": _dataclass_to_dict(signals.get("anomaly")) if signals.get("anomaly") else None,
            "spillover": _dataclass_to_dict(signals.get("spillover")) if signals.get("spillover") else None,
        }
