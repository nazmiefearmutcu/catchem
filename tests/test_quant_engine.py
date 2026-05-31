"""Tests for ``catchem.quant.engine.QuantEngine`` facade.

These tests pin contracts that the underlying signal modules don't own:
the cache TTL, the ``invalidate`` hook, the ``_QuoteAdapter`` shim, the
``capture_id`` not-found shortcut on ``novelty_for`` / ``reaction_for``,
and the recursive dataclass-to-dict serializer.

Each test owns a single contract so a regression there points at exactly
one expectation. Tests are fully deterministic — no real time/network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from catchem.quant.engine import QuantEngine, _dataclass_to_dict


class _StubStorage:
    """Minimal duck-typed storage with two seams: corpus + per-id lookup.

    The real storage is a SQLite-backed thing — that's out of scope here.
    We just need ``recent_records()`` to return a deterministic list and
    ``get_record()`` to look one up by id.
    """

    def __init__(self, records: list[dict] | None = None) -> None:
        self._records = list(records or [])
        self.recent_calls = 0
        self.get_calls = 0

    def recent_records(self, *, limit: int = 500, relevant_only: bool = False) -> list[dict]:
        self.recent_calls += 1
        return list(self._records[:limit])

    def get_record(self, capture_id: str) -> dict | None:
        self.get_calls += 1
        for r in self._records:
            if r.get("capture_id") == capture_id:
                return r
        return None


class _StubQuote:
    """A `MarketQuote`-shaped duck so ``_QuoteAdapter`` has something to wrap."""

    def __init__(self, symbol: str, last: float | None = 100.0) -> None:
        self.symbol = symbol
        self.last = last
        self.prev_close = 99.0
        self.error_code = None


class _StubProvider:
    """Provides ``quote(symbol)`` — the *real* provider's method name.

    The engine's ``_QuoteAdapter`` is what bridges this to the
    ``get_quote(symbol)`` shape that ``compute_reaction`` expects.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def quote(self, symbol: str) -> _StubQuote:
        self.calls.append(symbol)
        return _StubQuote(symbol)


# ---------------------------------------------------------------------------
# Cache contract
# ---------------------------------------------------------------------------


def test_quant_engine_cache_returns_same_object_within_ttl() -> None:
    """Two calls inside the TTL window must reuse the cached result.

    Pins the cache-hit branch in ``_cached`` (`now - ts < self._ttl`).
    Without this, every dashboard tick would re-run the signal compute.
    """

    storage = _StubStorage(records=[])
    engine = QuantEngine(storage=storage, market_provider=_StubProvider(), cache_ttl_seconds=60.0)

    first = engine.novelty_timeline(limit=10)
    second = engine.novelty_timeline(limit=10)

    # Same object identity ⇒ second call hit the cache (not just same value).
    assert first is second
    # And the storage was only consulted once for the corpus fetch.
    assert storage.recent_calls == 1


def test_quant_engine_invalidate_clears_cache() -> None:
    """``invalidate()`` (wired to the supervisor post-ingest hook) must
    drop every cached signal so the next call rebuilds from fresh data.

    Pins line 97 + the storage-read fan-out after invalidation.
    """

    storage = _StubStorage(records=[])
    engine = QuantEngine(storage=storage, market_provider=_StubProvider())

    engine.novelty_timeline(limit=10)
    engine.novelty_timeline(limit=10)
    assert storage.recent_calls == 1, "second call should have been a cache hit"

    engine.invalidate()
    engine.novelty_timeline(limit=10)
    assert storage.recent_calls == 2, "post-invalidate call must re-read storage"


# ---------------------------------------------------------------------------
# Missing-record shortcuts
# ---------------------------------------------------------------------------


def test_quant_engine_novelty_for_missing_record_returns_none() -> None:
    """``novelty_for(<unknown id>)`` must short-circuit to ``None`` and
    never reach the corpus fetch — the API surface relies on this for
    detail-drawer 404 handling.

    Pins lines 152-154.
    """

    storage = _StubStorage(records=[])
    engine = QuantEngine(storage=storage, market_provider=_StubProvider())

    result = engine.novelty_for("no-such-capture")

    assert result is None
    assert storage.get_calls == 1
    # Recent records must NOT have been pulled — short-circuit before corpus fetch.
    assert storage.recent_calls == 0


# ---------------------------------------------------------------------------
# Dataclass serializer
# ---------------------------------------------------------------------------


@dataclass
class _Nested:
    x: int


@dataclass
class _Outer:
    items: list[_Nested]
    by_key: dict[str, _Nested]


def test_dataclass_to_dict_recurses_through_lists_and_dicts() -> None:
    """``_dataclass_to_dict`` must descend into list/tuple/dict containers
    of dataclasses so the API response shape stays JSON-clean.

    Pins lines 62-66 — the list-vs-dict-vs-passthrough fanout.
    """

    outer = _Outer(items=[_Nested(1), _Nested(2)], by_key={"a": _Nested(3)})
    raw_list: list[Any] = [_Nested(4), 7, "lit"]
    raw_dict: dict[str, Any] = {"n": _Nested(5), "k": 9}

    assert _dataclass_to_dict(outer) == {
        "items": [{"x": 1}, {"x": 2}],
        "by_key": {"a": {"x": 3}},
    }
    assert _dataclass_to_dict(raw_list) == [{"x": 4}, 7, "lit"]
    assert _dataclass_to_dict(raw_dict) == {"n": {"x": 5}, "k": 9}
    # Primitive passthrough — not a dataclass, not a container.
    assert _dataclass_to_dict(42) == 42


# ── v72 diagnostics ─────────────────────────────────────────────────────


def test_safe_call_records_failure_into_diagnostics_buffer() -> None:
    """A signal that raises must:
    - return None to the caller (fail-soft contract preserved)
    - leave a structured entry in the ring buffer with class + message
    """
    from catchem.quant.engine import (
        _diagnostics_clear,
        _diagnostics_snapshot,
        _safe_call,
    )

    _diagnostics_clear()

    def boom() -> Any:
        raise ValueError("schema drift in upstream record")

    result = _safe_call(boom, label="event_clustering")
    assert result is None, "fail-soft contract — None on exception"

    snap = _diagnostics_snapshot()
    assert len(snap) == 1, "exactly one failure recorded"
    entry = snap[0]
    assert entry["signal"] == "event_clustering"
    assert entry["error_class"] == "ValueError"
    assert "schema drift" in entry["error"]
    assert entry.get("traceback_head")
    assert entry["elapsed_ms"] >= 0.0
    assert "ts" in entry


def test_diagnostics_ring_buffer_bounded_at_50() -> None:
    """Bursty failure storm must not eat memory — newest 50 kept, older drop."""
    from catchem.quant.engine import (
        _SIGNAL_FAILURES,
        _diagnostics_clear,
        _diagnostics_snapshot,
        _safe_call,
    )

    _diagnostics_clear()
    assert _SIGNAL_FAILURES.maxlen == 50

    for i in range(75):  # overflow by 25
        def crash(idx: int = i) -> Any:
            raise RuntimeError(f"burst-{idx}")
        _safe_call(crash, label=f"signal_{i % 3}")

    snap = _diagnostics_snapshot()
    assert len(snap) == 50, "ring buffer pinned at maxlen"
    # Oldest 25 should have dropped — the first surviving error is burst-25.
    assert "burst-25" in snap[0]["error"]
    # Newest is burst-74.
    assert "burst-74" in snap[-1]["error"]


def test_engine_diagnostics_aggregates_per_signal_counts(stub_storage) -> None:
    """``QuantEngine.diagnostics()`` rolls up failures into ``{signal: count}``
    and orders ``recent`` newest-first for the UI."""
    from catchem.quant.engine import _diagnostics_clear, _safe_call

    _diagnostics_clear()

    def crash_a() -> Any:
        raise KeyError("missing 'symbols' field")

    def crash_b() -> Any:
        raise ZeroDivisionError("zero-bucket window")

    _safe_call(crash_a, label="spillover")
    _safe_call(crash_a, label="spillover")
    _safe_call(crash_b, label="anomaly")

    engine = QuantEngine(storage=stub_storage)
    diag = engine.diagnostics()
    assert diag["total_failures"] == 3
    assert diag["per_signal"] == {"spillover": 2, "anomaly": 1}
    assert diag["buffer_capacity"] == 50
    # newest first → anomaly (KeyError of spillover came earlier)
    assert diag["recent"][0]["signal"] == "anomaly"
    assert diag["recent"][-1]["signal"] == "spillover"


def test_engine_diagnostics_empty_when_no_failures(stub_storage) -> None:
    """Healthy steady state: empty buffer, zero count, empty per_signal map."""
    from catchem.quant.engine import _diagnostics_clear

    _diagnostics_clear()
    engine = QuantEngine(storage=stub_storage)
    diag = engine.diagnostics()
    assert diag == {
        "total_failures": 0,
        "per_signal": {},
        "recent": [],
        "buffer_capacity": 50,
    }


@pytest.fixture
def stub_storage() -> Any:
    """Minimal storage seam reused across the v72 diagnostics tests."""
    return _StubStorage([])


# ---------------------------------------------------------------------------
# Realistic record corpus (the signal modules read FinancialImpactRecord
# dicts, not AwarenessCaptureView objects). Two near-duplicate Fed rows + one
# distinct equity row give every signal something non-trivial to chew on
# while staying tiny and fully deterministic.
# ---------------------------------------------------------------------------


def _record(
    capture_id: str,
    *,
    title: str,
    text: str,
    symbols: list[str],
    asset_classes: list[str],
    reasons: list[str],
    domain: str,
    published_ts: str,
) -> dict:
    return {
        "capture_id": capture_id,
        "doc_id": f"doc-{capture_id}",
        "title": title,
        "text_excerpt": text,
        "candidate_symbols": symbols,
        "asset_classes": asset_classes,
        "impact_reason_codes": reasons,
        "domain": domain,
        "published_ts": published_ts,
        "mean_relevance": 0.8,
    }


def _corpus() -> list[dict]:
    return [
        _record(
            "cap-fed-1",
            title="Fed raises rates by 25 bps amid sticky inflation",
            text="The Federal Reserve raised its benchmark interest rate citing inflation.",
            symbols=["SPY", "TLT"],
            asset_classes=["rates", "equities"],
            reasons=["monetary_policy"],
            domain="reuters.com",
            published_ts="2026-05-20T14:00:00Z",
        ),
        _record(
            "cap-fed-2",
            title="Federal Reserve hikes benchmark rate 25 bps on inflation",
            text="The Fed raised its benchmark rate by 25 basis points, citing inflation.",
            symbols=["SPY", "TLT"],
            asset_classes=["rates", "equities"],
            reasons=["monetary_policy"],
            domain="bloomberg.com",
            published_ts="2026-05-20T14:05:00Z",
        ),
        _record(
            "cap-aapl-1",
            title="Apple unveils new product line at developer event",
            text="Apple announced a refreshed hardware lineup and developer tooling.",
            symbols=["AAPL"],
            asset_classes=["equities"],
            reasons=["product_launch"],
            domain="theverge.com",
            published_ts="2026-05-20T16:00:00Z",
        ),
    ]


@pytest.fixture
def loaded_engine() -> QuantEngine:
    """Engine over the realistic 3-record corpus + the quote stub."""
    return QuantEngine(
        storage=_StubStorage(_corpus()),
        market_provider=_StubProvider(),
        cache_ttl_seconds=60.0,
    )


# ---------------------------------------------------------------------------
# Cache: TTL expiry + _recent_records reuse
# ---------------------------------------------------------------------------


def test_quant_engine_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call past the TTL window must rebuild — pins the false branch of
    ``(now - ts) < self._ttl`` in ``_cached`` (line 153->155).

    Time is driven by a monkeypatched ``time.time`` so the test is instant
    and deterministic — no real sleeping.
    """
    storage = _StubStorage(records=[])
    engine = QuantEngine(storage=storage, market_provider=_StubProvider(), cache_ttl_seconds=30.0)

    clock = {"now": 1000.0}
    monkeypatch.setattr("catchem.quant.engine.time.time", lambda: clock["now"])

    first = engine.novelty_timeline(limit=10)
    assert storage.recent_calls == 1

    # Still inside TTL → cache hit, same object, no new storage read.
    clock["now"] = 1020.0  # +20s < 30s
    second = engine.novelty_timeline(limit=10)
    assert second is first
    assert storage.recent_calls == 1

    # Past TTL → rebuild, fresh storage read.
    clock["now"] = 1031.0  # +31s > 30s
    third = engine.novelty_timeline(limit=10)
    assert storage.recent_calls == 2
    assert third is not first


def test_recent_records_reused_across_distinct_signals(loaded_engine: QuantEngine) -> None:
    """Several signals over the same ``limit`` share one corpus fetch — the
    whole point of caching ``_recent_records`` per (limit, relevant_only).
    """
    storage = loaded_engine._storage  # type: ignore[attr-defined]

    loaded_engine.clusters(limit=500)
    loaded_engine.regime(limit=500)
    loaded_engine.co_occurrence(limit=500)

    # All three pulled the same `records:500:False` corpus → one read total.
    assert storage.recent_calls == 1


# ---------------------------------------------------------------------------
# _QuoteAdapter shim
# ---------------------------------------------------------------------------


def test_quote_adapter_bridges_quote_to_get_quote() -> None:
    """``_QuoteAdapter.get_quote(symbol)`` must delegate to the provider's
    ``quote(symbol)`` — the rename bridge ``compute_reaction`` depends on.
    """
    from catchem.quant.engine import _QuoteAdapter

    provider = _StubProvider()
    adapter = _QuoteAdapter(provider)

    quote = adapter.get_quote("AAPL")

    assert provider.calls == ["AAPL"]
    assert quote.symbol == "AAPL"


def test_engine_defaults_to_local_fixture_provider() -> None:
    """When no ``market_provider`` is passed the engine wraps the bundled
    ``LocalFixtureMarketDataProvider`` (pins line 144's ``or`` fallback)."""
    from catchem.market_data import LocalFixtureMarketDataProvider

    engine = QuantEngine(storage=_StubStorage([]))
    assert isinstance(engine._market._provider, LocalFixtureMarketDataProvider)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Per-signal happy paths (each runs the real underlying module once)
# ---------------------------------------------------------------------------


def test_clusters_returns_event_clusters(loaded_engine: QuantEngine) -> None:
    """The two near-duplicate Fed rows (distinct domains) must cluster."""
    from catchem.quant.event_clustering import EventCluster

    clusters = loaded_engine.clusters(limit=500)
    assert isinstance(clusters, list)
    assert len(clusters) >= 1
    assert all(isinstance(c, EventCluster) for c in clusters)
    fed = max(clusters, key=lambda c: len(c.capture_ids))
    assert set(fed.capture_ids) == {"cap-fed-1", "cap-fed-2"}


def test_source_leaderboard_returns_report(loaded_engine: QuantEngine) -> None:
    from catchem.quant.source_reliability import SourceLeaderboard

    board = loaded_engine.source_leaderboard(limit=1000, min_records=1)
    assert isinstance(board, SourceLeaderboard)


def test_novelty_for_existing_record_returns_result(loaded_engine: QuantEngine) -> None:
    """Happy path of ``novelty_for`` — record found, corpus scored."""
    from catchem.quant.novelty import NoveltyResult

    result = loaded_engine.novelty_for("cap-aapl-1")
    assert isinstance(result, NoveltyResult)
    assert result.capture_id == "cap-aapl-1"
    # The distinct AAPL row is more novel than the near-duplicate Fed pair.
    assert result.novelty_score > 0.5


def test_novelty_timeline_returns_results(loaded_engine: QuantEngine) -> None:
    from catchem.quant.novelty import NoveltyResult

    timeline = loaded_engine.novelty_timeline(limit=200)
    assert isinstance(timeline, list)
    assert len(timeline) == 3
    assert all(isinstance(n, NoveltyResult) for n in timeline)


def test_lead_lag_returns_report_or_none(loaded_engine: QuantEngine) -> None:
    from catchem.quant.lead_lag import LeadLagReport

    report = loaded_engine.lead_lag(limit=500)
    assert report is None or isinstance(report, LeadLagReport)


def test_regime_runs(loaded_engine: QuantEngine) -> None:
    from catchem.quant.topic_regime import RegimeReport

    report = loaded_engine.regime(limit=1000, min_records_per_bucket=1)
    assert report is None or isinstance(report, RegimeReport)


def test_sentiment_momentum_runs(loaded_engine: QuantEngine) -> None:
    from catchem.quant.sentiment_momentum import SentimentMomentumReport

    report = loaded_engine.sentiment_momentum(limit=1000, min_mentions=1)
    assert report is None or isinstance(report, SentimentMomentumReport)


def test_co_occurrence_returns_report(loaded_engine: QuantEngine) -> None:
    from catchem.quant.co_occurrence import CoOccurrenceReport

    report = loaded_engine.co_occurrence(limit=1000, min_edge_weight=1)
    assert report is None or isinstance(report, CoOccurrenceReport)


def test_anomalies_runs(loaded_engine: QuantEngine) -> None:
    from catchem.quant.anomaly import AnomalyReport

    report = loaded_engine.anomalies(limit=1500)
    assert report is None or isinstance(report, AnomalyReport)


def test_spillover_runs(loaded_engine: QuantEngine) -> None:
    from catchem.quant.spillover import SpilloverReport

    report = loaded_engine.spillover(limit=1500)
    assert report is None or isinstance(report, SpilloverReport)


def test_reaction_for_existing_record_returns_report(loaded_engine: QuantEngine) -> None:
    """Happy path of ``reaction_for`` — record found, quote adapter used."""
    from catchem.quant.market_reaction import ReactionReport

    report = loaded_engine.reaction_for("cap-aapl-1")
    assert isinstance(report, ReactionReport)
    assert report.capture_id == "cap-aapl-1"


def test_reaction_for_missing_record_returns_none(loaded_engine: QuantEngine) -> None:
    """Unknown id short-circuits to None before building a cache key."""
    assert loaded_engine.reaction_for("no-such-id") is None


# ---------------------------------------------------------------------------
# None-fallback: list-returning signals coalesce a None signal to []
# ---------------------------------------------------------------------------


def test_clusters_none_signal_coalesces_to_empty_list(
    loaded_engine: QuantEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``cluster_records`` raises (→ _safe_call returns None), ``clusters``
    must still hand back ``[]`` — pins the ``... or []`` fail-soft tail."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("cluster boom")

    monkeypatch.setattr("catchem.quant.engine.cluster_records", boom)
    assert loaded_engine.clusters(limit=500) == []


def test_novelty_timeline_none_signal_coalesces_to_empty_list(
    loaded_engine: QuantEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("score boom")

    monkeypatch.setattr("catchem.quant.engine.score_corpus", boom)
    assert loaded_engine.novelty_timeline(limit=200) == []


def test_source_leaderboard_none_on_signal_failure(
    loaded_engine: QuantEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report-returning signals propagate ``None`` (no ``or []`` tail)."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("sources boom")

    monkeypatch.setattr("catchem.quant.engine.compute_source_scores", boom)
    assert loaded_engine.source_leaderboard(limit=1000) is None


# ---------------------------------------------------------------------------
# dashboard_snapshot: ThreadPoolExecutor fan-out + serialization
# ---------------------------------------------------------------------------


def test_dashboard_snapshot_fans_out_and_serializes(loaded_engine: QuantEngine) -> None:
    """One call fills the full payload shape with JSON-clean (dict / None)
    values for every one of the 10 signal slots."""
    snap = loaded_engine.dashboard_snapshot(limit=500)

    assert snap["n_records_window"] == 3
    assert snap["n_clusters"] >= 1
    # clusters serialized to plain dicts (no dataclass leaks).
    assert isinstance(snap["clusters"], list)
    assert all(isinstance(c, dict) for c in snap["clusters"])

    expected_keys = {
        "n_records_window",
        "n_clusters",
        "clusters",
        "source_leaderboard",
        "novelty_timeline",
        "lead_lag",
        "regime",
        "sentiment_momentum",
        "co_occurrence",
        "anomalies",
        "spillover",
    }
    assert set(snap) == expected_keys

    # novelty_timeline always a list of dicts; every report slot is dict|None.
    assert isinstance(snap["novelty_timeline"], list)
    assert all(isinstance(n, dict) for n in snap["novelty_timeline"])
    for slot in (
        "source_leaderboard",
        "lead_lag",
        "regime",
        "sentiment_momentum",
        "co_occurrence",
        "anomalies",
        "spillover",
    ):
        assert snap[slot] is None or isinstance(snap[slot], dict)


def test_dashboard_snapshot_survives_one_broken_signal(
    loaded_engine: QuantEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft contract end to end: a single crashing module must NOT
    blow up the whole dashboard — its slot just goes ``None``/``[]``."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("spillover down")

    monkeypatch.setattr("catchem.quant.engine.compute_spillover", boom)

    snap = loaded_engine.dashboard_snapshot(limit=500)
    assert snap["spillover"] is None
    # Other signals unaffected.
    assert snap["n_clusters"] >= 1


def test_dashboard_snapshot_cache_hit_skips_recompute(loaded_engine: QuantEngine) -> None:
    """Second snapshot within TTL reuses cached signals — storage corpus is
    read exactly once across both calls."""
    storage = loaded_engine._storage  # type: ignore[attr-defined]

    loaded_engine.dashboard_snapshot(limit=500)
    reads_after_first = storage.recent_calls
    loaded_engine.dashboard_snapshot(limit=500)

    assert storage.recent_calls == reads_after_first


def test_cache_is_thread_safe_under_concurrent_signal_fanout(
    loaded_engine: QuantEngine,
) -> None:
    """The `_cache` dict is mutated by many worker threads (dashboard_snapshot
    fans signals out across a ThreadPoolExecutor). With the cache lock in
    place, hammering the engine from many threads must not raise, corrupt the
    dict, or interleave invalidate() with reads. Pins the `_cache_lock` guard.

    Without the lock this can intermittently throw "dictionary changed size
    during iteration" / "RuntimeError" or drop entries on a concurrent rehash.
    """
    from concurrent.futures import ThreadPoolExecutor

    errors: list[BaseException] = []

    def hammer(i: int) -> None:
        try:
            # Mix cache reads, fresh computes, full fan-out, and invalidation
            # so the lock is exercised on every path concurrently.
            loaded_engine.clusters(limit=500)
            loaded_engine.novelty_timeline(limit=200)
            loaded_engine.dashboard_snapshot(limit=500)
            if i % 5 == 0:
                loaded_engine.invalidate()
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(hammer, range(40)))

    assert not errors, f"concurrent cache access raised: {errors[:3]}"
    # Engine still usable + cache internally consistent after the storm.
    snap = loaded_engine.dashboard_snapshot(limit=500)
    assert "n_clusters" in snap
