"""Tests for ``catchem.quant.event_clustering``.

The module is pure-function and dependency-free, so these tests work on
hand-rolled dicts that mimic the FinancialImpactRecord shape — no fixtures,
no storage, no env wiring required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from catchem.quant.event_clustering import (
    EventCluster,
    cluster_records,
    pairwise_similarity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """ISO-8601 with explicit UTC offset."""
    return dt.astimezone(timezone.utc).isoformat()


def _rec(
    capture_id: str,
    *,
    title: str = "",
    symbols: list[str] | None = None,
    reasons: list[str] | None = None,
    assets: list[str] | None = None,
    entities: list[str] | None = None,
    domain: str = "example.com",
    published_ts: datetime | str | None = None,
    created_at: datetime | str | None = None,
    relevance: float = 0.8,
    is_finance: bool = True,
    sentiment_label: str | None = "neutral",
    sentiment_score: float | None = 0.5,
) -> dict[str, Any]:
    """Build a FinancialImpactRecord-shaped dict."""
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    if isinstance(published_ts, datetime):
        published_ts = _iso(published_ts)
    if isinstance(created_at, datetime):
        created_at = _iso(created_at)
    if created_at is None:
        created_at = _iso(now)
    return {
        "capture_id": capture_id,
        "doc_id": f"doc-{capture_id}",
        "title": title,
        "text_excerpt": title,
        "domain": domain,
        "language": "en",
        "url": f"https://{domain}/{capture_id}",
        "is_finance_relevant": is_finance,
        "finance_relevance_score": relevance,
        "asset_classes": assets or [],
        "impact_reason_codes": reasons or [],
        "candidate_symbols": symbols or [],
        "candidate_entities": entities or [],
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "published_ts": published_ts,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Core required test cases
# ---------------------------------------------------------------------------


def test_empty_list_returns_empty():
    assert cluster_records([]) == []


def test_fed_rate_hike_clusters_into_one_event():
    """Five outlets covering a Fed rate hike within ~10min cluster together."""
    base = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    outlets = ["reuters.com", "bloomberg.com", "wsj.com", "ft.com", "cnbc.com"]
    titles = [
        "Fed raises rates by 25 basis points",
        "Federal Reserve hikes interest rates 25bps",
        "Fed lifts benchmark rate amid inflation fight",
        "US central bank raises rates as inflation persists",
        "Fed delivers 25 basis point rate hike",
    ]
    records = [
        _rec(
            f"cap-fed-{i}",
            title=titles[i],
            symbols=["SPY", "TLT", "DXY"],
            reasons=["MONETARY_POLICY", "RATE_HIKE"],
            assets=["EQUITY", "RATES"],
            entities=["Federal Reserve", "FOMC"],
            domain=outlets[i],
            published_ts=base + timedelta(minutes=2 * i),
            relevance=0.95,
        )
        for i in range(5)
    ]

    clusters = cluster_records(records)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.size == 5
    assert set(cluster.capture_ids) == {f"cap-fed-{i}" for i in range(5)}
    # All five outlets present.
    assert set(cluster.member_domains) == set(outlets)


def test_unrelated_records_do_not_cluster():
    """Sports article + Fed announcement → no cluster (both become singletons,
    both dropped since min_cluster_size=2 by default)."""
    base = datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc)
    fed = _rec(
        "cap-fed",
        title="Fed hikes interest rates 25 basis points",
        symbols=["SPY", "TLT"],
        reasons=["MONETARY_POLICY"],
        assets=["RATES"],
        entities=["Federal Reserve"],
        domain="reuters.com",
        published_ts=base,
    )
    sports = _rec(
        "cap-sports",
        title="Lakers defeat Celtics in overtime thriller",
        symbols=[],
        reasons=[],
        assets=[],
        entities=["Lakers", "Celtics"],
        domain="espn.com",
        published_ts=base + timedelta(minutes=5),
        is_finance=False,
        relevance=0.0,
    )

    assert cluster_records([fed, sports]) == []

    # Sanity: similarity is below threshold even without the time window.
    assert pairwise_similarity(fed, sports) < 0.35


def test_record_outside_time_window_does_not_cluster():
    """A late-arriving outlet beyond ``window_seconds`` becomes its own
    cluster even though the content matches."""
    base = datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc)
    common = dict(
        title="Apple unveils new MacBook Pro lineup",
        symbols=["AAPL"],
        reasons=["PRODUCT_LAUNCH"],
        assets=["EQUITY"],
        entities=["Apple"],
    )
    r1 = _rec("cap-a", domain="reuters.com", published_ts=base, **common)
    r2 = _rec(
        "cap-b", domain="bloomberg.com", published_ts=base + timedelta(minutes=5), **common
    )
    # 90 minutes later — well past the default 30 min window.
    r3 = _rec(
        "cap-c", domain="cnbc.com", published_ts=base + timedelta(minutes=90), **common
    )

    clusters = cluster_records([r1, r2, r3], window_seconds=1800)
    assert len(clusters) == 1, "only the in-window pair should cluster"
    assert set(clusters[0].capture_ids) == {"cap-a", "cap-b"}

    # Widen window → all three cluster.
    clusters_wide = cluster_records([r1, r2, r3], window_seconds=2 * 3600)
    assert len(clusters_wide) == 1
    assert set(clusters_wide[0].capture_ids) == {"cap-a", "cap-b", "cap-c"}


def test_cluster_id_is_deterministic():
    """Running the same input twice yields identical cluster_ids, and the
    SHA-1 is stable across orderings of the input list."""
    base = datetime(2026, 5, 27, 11, 0, tzinfo=timezone.utc)
    records = [
        _rec(
            f"cap-{i}",
            title="ECB cuts rates by 25 basis points to support growth",
            symbols=["EURUSD", "DBK"],
            reasons=["MONETARY_POLICY", "RATE_CUT"],
            assets=["RATES", "FX"],
            entities=["European Central Bank"],
            domain=f"site-{i}.com",
            published_ts=base + timedelta(minutes=i),
        )
        for i in range(3)
    ]

    a = cluster_records(records)
    b = cluster_records(list(reversed(records)))
    assert len(a) == 1 and len(b) == 1
    assert a[0].cluster_id == b[0].cluster_id

    # Hex SHA-1 is 40 chars.
    assert len(a[0].cluster_id) == 40
    assert all(ch in "0123456789abcdef" for ch in a[0].cluster_id)


def test_dominant_symbols_ranked_by_frequency():
    """Most-frequent symbols appear first; symbols seen in only 1 member
    are dropped from the dominant list."""
    base = datetime(2026, 5, 27, 13, 0, tzinfo=timezone.utc)
    # NVDA appears in all 4, AAPL in 3, MSFT in 2, TSLA only in 1 (dropped).
    records = [
        _rec(
            "cap-1",
            title="Tech stocks rally on AI optimism",
            symbols=["NVDA", "AAPL", "MSFT", "TSLA"],
            reasons=["MARKET_REACTION"],
            assets=["EQUITY"],
            entities=["NVIDIA"],
            published_ts=base,
        ),
        _rec(
            "cap-2",
            title="AI optimism lifts NVDA and AAPL",
            symbols=["NVDA", "AAPL", "MSFT"],
            reasons=["MARKET_REACTION"],
            assets=["EQUITY"],
            entities=["NVIDIA"],
            published_ts=base + timedelta(minutes=3),
        ),
        _rec(
            "cap-3",
            title="NVDA leads tech stocks on AI rally",
            symbols=["NVDA", "AAPL"],
            reasons=["MARKET_REACTION"],
            assets=["EQUITY"],
            entities=["NVIDIA"],
            published_ts=base + timedelta(minutes=6),
        ),
        _rec(
            "cap-4",
            title="Chip stocks rally led by NVDA on AI demand",
            symbols=["NVDA"],
            reasons=["MARKET_REACTION"],
            assets=["EQUITY"],
            entities=["NVIDIA"],
            published_ts=base + timedelta(minutes=9),
        ),
    ]
    clusters = cluster_records(records)
    assert len(clusters) == 1
    dom = clusters[0].dominant_symbols
    # NVDA (4) > AAPL (3) > MSFT (2). TSLA (1) dropped.
    assert dom[0] == "NVDA"
    assert dom[1] == "AAPL"
    assert dom[2] == "MSFT"
    assert "TSLA" not in dom


def test_coherence_reasonable_for_tight_cluster():
    """A near-identical group of headlines yields coherence well above 0.5."""
    base = datetime(2026, 5, 27, 15, 0, tzinfo=timezone.utc)
    records = [
        _rec(
            f"cap-tight-{i}",
            title="Fed raises rates 25 basis points",
            symbols=["SPY", "TLT"],
            reasons=["MONETARY_POLICY", "RATE_HIKE"],
            assets=["RATES", "EQUITY"],
            entities=["Federal Reserve", "FOMC"],
            domain=f"site-{i}.com",
            published_ts=base + timedelta(minutes=i),
        )
        for i in range(4)
    ]
    clusters = cluster_records(records)
    assert len(clusters) == 1
    assert clusters[0].coherence >= 0.5
    # Mean relevance should also reflect the input.
    assert clusters[0].mean_relevance == pytest.approx(0.8, abs=0.001)


# ---------------------------------------------------------------------------
# Edge cases beyond the required set
# ---------------------------------------------------------------------------


def test_missing_published_ts_falls_back_to_created_at():
    base = datetime(2026, 5, 27, 16, 0, tzinfo=timezone.utc)
    r1 = _rec(
        "cap-x1",
        title="Apple raises iPhone prices in Europe",
        symbols=["AAPL"],
        reasons=["PRICING"],
        assets=["EQUITY"],
        entities=["Apple"],
        published_ts=None,
        created_at=base,
    )
    r2 = _rec(
        "cap-x2",
        title="Apple lifts iPhone prices across Europe",
        symbols=["AAPL"],
        reasons=["PRICING"],
        assets=["EQUITY"],
        entities=["Apple"],
        published_ts=None,
        created_at=base + timedelta(minutes=4),
    )
    clusters = cluster_records([r1, r2])
    assert len(clusters) == 1
    assert set(clusters[0].capture_ids) == {"cap-x1", "cap-x2"}


def test_min_cluster_size_one_keeps_singletons():
    base = datetime(2026, 5, 27, 17, 0, tzinfo=timezone.utc)
    r = _rec(
        "cap-only",
        title="One-off headline about something obscure",
        symbols=["XYZ"],
        published_ts=base,
    )
    out = cluster_records([r], min_cluster_size=1)
    assert len(out) == 1
    assert out[0].size == 1
    assert out[0].coherence == pytest.approx(1.0)


def test_size_two_coherence_equals_single_pair_score():
    base = datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)
    a = _rec(
        "pa",
        title="Tesla beats earnings expectations on strong deliveries",
        symbols=["TSLA"],
        reasons=["EARNINGS"],
        assets=["EQUITY"],
        entities=["Tesla"],
        published_ts=base,
    )
    b = _rec(
        "pb",
        title="Tesla earnings top expectations with record deliveries",
        symbols=["TSLA"],
        reasons=["EARNINGS"],
        assets=["EQUITY"],
        entities=["Tesla"],
        published_ts=base + timedelta(minutes=2),
    )
    clusters = cluster_records([a, b])
    assert len(clusters) == 1
    pair_score = pairwise_similarity(a, b)
    assert clusters[0].coherence == pytest.approx(pair_score)


def test_non_finance_records_still_cluster_on_text_and_symbols():
    """Records flagged ``is_finance_relevant=False`` still cluster if they
    share enough signal — the module is a pure clusterer, not a filter."""
    base = datetime(2026, 5, 27, 19, 0, tzinfo=timezone.utc)
    common = dict(
        symbols=["BTC"],
        title="Bitcoin tops 100000 dollars amid bullish sentiment",
        is_finance=False,
        relevance=0.0,
    )
    r1 = _rec("nf-1", domain="a.com", published_ts=base, **common)
    r2 = _rec(
        "nf-2",
        domain="b.com",
        published_ts=base + timedelta(minutes=3),
        **common,
    )
    out = cluster_records([r1, r2])
    assert len(out) == 1
    assert set(out[0].capture_ids) == {"nf-1", "nf-2"}


def test_does_not_crash_on_none_fields():
    """Defensive: a record with all optional list fields = None must not raise."""
    base = datetime(2026, 5, 27, 20, 0, tzinfo=timezone.utc)
    sparse = {
        "capture_id": "sparse",
        "title": None,
        "domain": None,
        "asset_classes": None,
        "impact_reason_codes": None,
        "candidate_symbols": None,
        "candidate_entities": None,
        "finance_relevance_score": None,
        "published_ts": None,
        "created_at": _iso(base),
    }
    twin = dict(sparse)
    twin["capture_id"] = "sparse-2"
    # Single record alone → dropped because min_cluster_size=2 default; the
    # pair of two empty records has no overlapping signal → similarity 0 →
    # no clustering. Important: no crash either way.
    assert cluster_records([sparse]) == []
    assert cluster_records([sparse, twin]) == []


def test_returns_eventcluster_instances():
    """Public API surface check: list[EventCluster], frozen dataclasses."""
    base = datetime(2026, 5, 27, 21, 0, tzinfo=timezone.utc)
    pair = [
        _rec(
            "pc-1",
            title="Fed cuts rates",
            symbols=["SPY"],
            reasons=["RATE_CUT"],
            published_ts=base,
        ),
        _rec(
            "pc-2",
            title="Fed cuts rates again",
            symbols=["SPY"],
            reasons=["RATE_CUT"],
            published_ts=base + timedelta(minutes=2),
        ),
    ]
    out = cluster_records(pair)
    assert len(out) == 1
    assert isinstance(out[0], EventCluster)
    # frozen=True → cannot mutate.
    with pytest.raises(Exception):
        out[0].size = 99  # type: ignore[misc]


def test_module_import_surface():
    """Imports declared in the task brief must work."""
    from catchem.quant.event_clustering import cluster_records as fn  # noqa: F401
    from catchem.quant.event_clustering import EventCluster as EC  # noqa: F401


def test_pairwise_similarity_identical_dict_reference_is_one():
    """Same dict identity short-circuits the five-channel sum to ``1.0``.

    Pins line 204 (``a is b`` branch) — without this every signal-weight
    constant change would risk a non-1.0 self-similarity that breaks the
    clustering greedy step.
    """

    rec = _rec(
        "self-1",
        title="Fed surprises with 50bp cut",
        symbols=["SPY", "TLT"],
        reasons=["rate-cut"],
    )
    # Pass the SAME object on both sides — branch only fires on identity.
    assert pairwise_similarity(rec, rec) == 1.0
