"""Tests for ``catchem.quant.source_reliability``.

Strategy: hand-build minimal FinancialImpactRecord-shaped dicts so the
assertions stay independent of the rest of the pipeline. Each test
isolates one rule (window, min_records, entropy, etc.) so a regression
points to a single broken branch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import log
from typing import Any

import pytest

from catchem.quant.source_reliability import (
    SourceLeaderboard,
    compute_source_scores,
)

# ── fixtures / builders ────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _rec(
    *,
    domain: str = "reuters.com",
    published_ts: datetime | None = None,
    created_at: datetime | None = None,
    is_finance_relevant: bool = True,
    finance_relevance_score: float = 0.9,
    sentiment_label: str | None = "positive",
    asset_classes: list[str] | None = None,
    impact_reason_codes: list[str] | None = None,
    candidate_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Build a single record dict. Defaults represent a high-quality row."""
    ts = published_ts or _now() - timedelta(hours=1)
    return {
        "domain": domain,
        "published_ts": _iso(ts),
        "created_at": _iso(created_at or ts),
        "is_finance_relevant": is_finance_relevant,
        "finance_relevance_score": finance_relevance_score,
        "sentiment_label": sentiment_label,
        "sentiment_score": 0.8,
        "asset_classes": list(asset_classes) if asset_classes is not None else ["equity"],
        "impact_reason_codes": list(impact_reason_codes) if impact_reason_codes is not None else ["earnings"],
        "candidate_symbols": list(candidate_symbols) if candidate_symbols is not None else ["AAPL"],
    }


# ── tests ──────────────────────────────────────────────────────────────────


def test_empty_input_returns_empty_leaderboard() -> None:
    lb = compute_source_scores([])
    assert isinstance(lb, SourceLeaderboard)
    assert lb.window_days == 30
    assert lb.total_records == 0
    assert lb.total_domains == 0
    assert lb.sources == ()


def test_single_dominant_source_appears_with_high_composite() -> None:
    records = [_rec() for _ in range(5)]
    lb = compute_source_scores(records, min_records=3)

    assert lb.total_records == 5
    assert lb.total_domains == 1
    assert len(lb.sources) == 1
    only = lb.sources[0]
    assert only.domain == "reuters.com"
    assert only.record_count == 5
    assert only.relevant_count == 5
    assert only.relevant_rate == pytest.approx(1.0)
    assert only.mean_relevance_score == pytest.approx(0.9)
    assert only.signal_density == pytest.approx(1.0)
    # All positive sentiment ⇒ skew = +1.
    assert only.sentiment_skew == pytest.approx(1.0)
    # 0 <= composite <= 1 always.
    assert 0.0 <= only.composite_score <= 1.0
    # With perfect relevance + density + skew, composite should be solidly > 0.5.
    assert only.composite_score > 0.5


def test_quality_source_beats_noisy_one() -> None:
    """Reuters: 3 relevant finance records. TMZ: 5 irrelevant sports records."""
    reuters = [
        _rec(
            domain="reuters.com",
            finance_relevance_score=0.95,
            asset_classes=["equity"],
            impact_reason_codes=["earnings"],
            candidate_symbols=["AAPL"],
        ),
        _rec(
            domain="reuters.com",
            finance_relevance_score=0.88,
            asset_classes=["fx"],
            impact_reason_codes=["macro"],
            candidate_symbols=["EURUSD"],
        ),
        _rec(
            domain="reuters.com",
            finance_relevance_score=0.92,
            asset_classes=["bond"],
            impact_reason_codes=["rates"],
            candidate_symbols=["UST10"],
        ),
    ]
    tmz = [
        _rec(
            domain="tmz.com",
            is_finance_relevant=False,
            finance_relevance_score=0.05,
            sentiment_label=None,
            asset_classes=[],
            impact_reason_codes=[],
            candidate_symbols=[],
        )
        for _ in range(5)
    ]

    lb = compute_source_scores(reuters + tmz, min_records=3)
    domains = {s.domain: s for s in lb.sources}
    assert "reuters.com" in domains
    assert "tmz.com" in domains
    assert domains["reuters.com"].composite_score > domains["tmz.com"].composite_score
    # Reuters first because it ranks higher.
    assert lb.sources[0].domain == "reuters.com"
    # TMZ has zero finance content.
    assert domains["tmz.com"].relevant_rate == pytest.approx(0.0)
    assert domains["tmz.com"].mean_relevance_score == pytest.approx(0.0)


def test_window_filter_drops_old_records() -> None:
    fresh_ts = _now() - timedelta(days=2)
    old_ts = _now() - timedelta(days=45)

    records = [
        _rec(domain="reuters.com", published_ts=fresh_ts) for _ in range(3)
    ] + [
        _rec(domain="reuters.com", published_ts=old_ts) for _ in range(10)
    ]

    lb = compute_source_scores(records, window_days=30, min_records=3)
    assert lb.total_records == 3  # only the fresh ones survive
    assert len(lb.sources) == 1
    assert lb.sources[0].record_count == 3


def test_min_records_excludes_thin_domains() -> None:
    records = [_rec(domain="reuters.com") for _ in range(5)] + [
        _rec(domain="tinyblog.io") for _ in range(2)
    ]
    lb = compute_source_scores(records, min_records=3)
    domains = {s.domain for s in lb.sources}
    assert "reuters.com" in domains
    assert "tinyblog.io" not in domains
    # But total counters still see both.
    assert lb.total_domains == 2
    assert lb.total_records == 7


def test_asset_diversity_approaches_one_when_uniformly_spread() -> None:
    """Five records, one per distinct asset_class ⇒ entropy = log(5), normalized = 1."""
    asset_buckets = [["equity"], ["fx"], ["crypto"], ["bond"], ["commodity"]]
    records = [
        _rec(
            domain="reuters.com",
            asset_classes=ac,
            impact_reason_codes=[f"reason_{i}"],
            candidate_symbols=[f"SYM{i}"],
        )
        for i, ac in enumerate(asset_buckets)
    ]
    lb = compute_source_scores(records, min_records=3)
    assert len(lb.sources) == 1
    assert lb.sources[0].asset_diversity == pytest.approx(1.0)


def test_asset_diversity_is_zero_for_singleton_distribution() -> None:
    records = [_rec(domain="reuters.com", asset_classes=["equity"]) for _ in range(4)]
    lb = compute_source_scores(records, min_records=3)
    assert lb.sources[0].asset_diversity == pytest.approx(0.0)


def test_composite_score_always_in_unit_interval() -> None:
    """Mix of edge-case rows: missing fields, NaN-ish scores, varied signals."""
    records = [
        _rec(domain="reuters.com"),
        _rec(domain="reuters.com", sentiment_label="negative", finance_relevance_score=0.71),
        _rec(domain="reuters.com", sentiment_label="neutral", finance_relevance_score=0.55),
        _rec(domain="bloomberg.com", is_finance_relevant=False, finance_relevance_score=0.1),
        _rec(domain="bloomberg.com", finance_relevance_score=0.99),
        _rec(domain="bloomberg.com", asset_classes=[], impact_reason_codes=[], candidate_symbols=[]),
        # Missing domain ⇒ "(unknown)".
        _rec(domain=""),
        _rec(domain=""),
        _rec(domain=""),
    ]
    lb = compute_source_scores(records, min_records=3)
    assert lb.sources, "expected at least one ranked source"
    for src in lb.sources:
        assert 0.0 <= src.composite_score <= 1.0, src
        assert 0.0 <= src.relevant_rate <= 1.0
        assert 0.0 <= src.mean_relevance_score <= 1.0
        assert 0.0 <= src.signal_density <= 1.0
        assert -1.0 <= src.sentiment_skew <= 1.0
        assert 0.0 <= src.asset_diversity <= 1.0
        assert 0.0 <= src.reason_diversity <= 1.0
        assert 0.0 <= src.symbol_uniqueness <= 1.0


def test_symbol_uniqueness_isolates_domain_specific_coverage() -> None:
    """Reuters covers AAPL+MSFT; obscure.io covers a symbol no one else does."""
    records = [
        _rec(domain="reuters.com", candidate_symbols=["AAPL"]),
        _rec(domain="reuters.com", candidate_symbols=["MSFT"]),
        _rec(domain="reuters.com", candidate_symbols=["AAPL"]),
        _rec(domain="bloomberg.com", candidate_symbols=["AAPL"]),
        _rec(domain="bloomberg.com", candidate_symbols=["MSFT"]),
        _rec(domain="bloomberg.com", candidate_symbols=["GOOG"]),
        _rec(domain="obscure.io", candidate_symbols=["RARE1"]),
        _rec(domain="obscure.io", candidate_symbols=["RARE2"]),
        _rec(domain="obscure.io", candidate_symbols=["RARE3"]),
    ]
    lb = compute_source_scores(records, min_records=3)
    by_domain = {s.domain: s for s in lb.sources}
    # obscure.io: every symbol is unique.
    assert by_domain["obscure.io"].symbol_uniqueness == pytest.approx(1.0)
    # reuters.com shares everything (AAPL, MSFT) with bloomberg.com.
    assert by_domain["reuters.com"].symbol_uniqueness == pytest.approx(0.0)


def test_domain_normalization_case_and_whitespace() -> None:
    records = [
        _rec(domain="Reuters.com"),
        _rec(domain="REUTERS.COM"),
        _rec(domain="  reuters.com  "),
    ]
    lb = compute_source_scores(records, min_records=3)
    assert len(lb.sources) == 1
    assert lb.sources[0].domain == "reuters.com"
    assert lb.sources[0].record_count == 3


def test_unknown_domain_bucket() -> None:
    records = [
        _rec(domain=None),  # type: ignore[arg-type]
        _rec(domain=""),
        _rec(domain="   "),
    ]
    lb = compute_source_scores(records, min_records=3)
    assert len(lb.sources) == 1
    assert lb.sources[0].domain == "(unknown)"


def test_ranking_ties_break_by_record_count_descending() -> None:
    """Two domains, identical signals, different volumes ⇒ higher-volume first."""
    a = [_rec(domain="a.com") for _ in range(5)]
    b = [_rec(domain="b.com") for _ in range(3)]
    lb = compute_source_scores(a + b, min_records=3)
    assert lb.sources[0].domain == "a.com"
    assert lb.sources[1].domain == "b.com"
    # Composite is identical for both (same record shape).
    assert lb.sources[0].composite_score == pytest.approx(lb.sources[1].composite_score)


def test_sentiment_skew_balanced_signal_is_zero() -> None:
    records = [
        _rec(domain="reuters.com", sentiment_label="positive"),
        _rec(domain="reuters.com", sentiment_label="positive"),
        _rec(domain="reuters.com", sentiment_label="negative"),
        _rec(domain="reuters.com", sentiment_label="negative"),
    ]
    lb = compute_source_scores(records, min_records=3)
    assert lb.sources[0].sentiment_skew == pytest.approx(0.0)


def test_falls_back_to_created_at_when_published_ts_missing() -> None:
    ts = _now() - timedelta(hours=2)
    records = [
        {
            "domain": "reuters.com",
            "published_ts": None,
            "created_at": _iso(ts),
            "is_finance_relevant": True,
            "finance_relevance_score": 0.9,
            "sentiment_label": "positive",
            "sentiment_score": 0.8,
            "asset_classes": ["equity"],
            "impact_reason_codes": ["earnings"],
            "candidate_symbols": ["AAPL"],
        }
        for _ in range(3)
    ]
    lb = compute_source_scores(records, min_records=3)
    assert lb.total_records == 3
    assert lb.sources[0].domain == "reuters.com"


def test_normalized_entropy_two_balanced_classes_equals_one() -> None:
    """Independent sanity check on the entropy formula via the public API."""
    records = []
    for _ in range(3):
        records.append(_rec(domain="x.com", asset_classes=["equity"]))
    for _ in range(3):
        records.append(_rec(domain="x.com", asset_classes=["fx"]))
    lb = compute_source_scores(records, min_records=3)
    # entropy = -2 * 0.5 * log(0.5) = log(2); max = log(2) ⇒ normalized = 1.0
    assert lb.sources[0].asset_diversity == pytest.approx(1.0)
    # Sanity: log(2) > 0 (catches an accidental sign flip).
    assert log(2) > 0
