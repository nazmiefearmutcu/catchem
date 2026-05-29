"""Unit tests for the pure portfolio enrichment join.

Exercises :func:`catchem.portfolio.enrich_holdings` with injected records, a
fake ``quote_fn``, and a fixed ``now`` so behaviour is fully deterministic.
Covers: news-count classification (in-window vs stale), recent_top score
ordering + cap, coverage covered/blind-spot freshness, quote normalization
(dict + object + failure), and tolerance of malformed input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from catchem.portfolio import enrich_holdings

NOW = datetime(2024, 1, 2, 21, 0, tzinfo=UTC)


def _rec(
    *,
    title: str = "",
    text: str = "",
    symbols: list[str] | None = None,
    score: float = 0.0,
    age_seconds: float = 60.0,
    url: str | None = None,
) -> dict:
    """Build a tolerant record dict with a published_ts `age_seconds` before NOW."""
    return {
        "title": title,
        "text": text,
        "candidate_symbols": symbols or [],
        "finance_relevance_score": score,
        "published_ts": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "url": url,
    }


class _FakeQuote:
    """Object-style quote (mimics MarketQuote attribute access)."""

    def __init__(self, last, prev_close, change_pct=None):
        self.last = last
        self.prev_close = prev_close
        self.change_pct = change_pct


def test_news_count_only_counts_in_window_matches() -> None:
    records = [
        _rec(title="AAPL beats earnings", symbols=["AAPL"], age_seconds=100),
        _rec(text="Apple ships AAPL chips", age_seconds=200),  # text substring
        _rec(title="AAPL last year", symbols=["AAPL"], age_seconds=999_999),  # stale
        _rec(title="MSFT only", symbols=["MSFT"], age_seconds=100),  # other symbol
    ]
    out = enrich_holdings(
        [{"symbol": "AAPL"}],
        records=records,
        quote_fn=lambda s: None,
        now=NOW,
        window_seconds=86400.0,
    )
    assert len(out) == 1
    assert out[0]["recent_news_count"] == 2  # two in-window matches


def test_recent_top_sorted_by_score_desc_and_capped_at_three() -> None:
    records = [
        _rec(title="low", symbols=["TSLA"], score=0.1, url="u1"),
        _rec(title="high", symbols=["TSLA"], score=0.9, url="u2"),
        _rec(title="mid", symbols=["TSLA"], score=0.5, url="u3"),
        _rec(title="mid2", symbols=["TSLA"], score=0.4, url="u4"),
    ]
    out = enrich_holdings(
        [{"symbol": "TSLA"}], records=records, quote_fn=lambda s: None, now=NOW
    )
    top = out[0]["recent_top"]
    assert len(top) == 3  # capped
    assert [t["score"] for t in top] == [0.9, 0.5, 0.4]  # desc
    assert top[0] == {"title": "high", "url": "u2", "score": 0.9}


def test_coverage_covered_vs_blind_spot() -> None:
    records = [_rec(title="NVDA surges", symbols=["NVDA"], age_seconds=120)]
    out = enrich_holdings(
        [{"symbol": "NVDA"}, {"symbol": "GME"}],
        records=records,
        quote_fn=lambda s: None,
        now=NOW,
    )
    by_sym = {h["symbol"]: h for h in out}
    assert by_sym["NVDA"]["coverage"]["covered"] is True
    assert by_sym["NVDA"]["coverage"]["mention_count"] == 1
    assert by_sym["NVDA"]["coverage"]["last_seen_age_seconds"] == 120.0
    # GME has zero coverage → blind spot.
    assert by_sym["GME"]["coverage"] == {
        "covered": False,
        "last_seen_age_seconds": None,
        "mention_count": 0,
    }


def test_quote_from_object_and_dict_and_failure() -> None:
    def quote_fn(symbol: str):
        if symbol == "AAPL":
            return _FakeQuote(last=190.0, prev_close=188.0)  # change_pct derived
        if symbol == "MSFT":
            return {"last": 370.0, "prev_close": 370.0, "change_pct": 0.0}
        raise RuntimeError("provider exploded")  # symbol == "BOOM"

    out = enrich_holdings(
        [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "BOOM"}],
        records=[],
        quote_fn=quote_fn,
        now=NOW,
    )
    by_sym = {h["symbol"]: h for h in out}
    # Object quote: change_pct derived from last/prev_close.
    assert by_sym["AAPL"]["quote"]["last"] == 190.0
    assert abs(by_sym["AAPL"]["quote"]["change_pct"] - (2.0 / 188.0)) < 1e-9
    # Dict quote passes through.
    assert by_sym["MSFT"]["quote"] == {"last": 370.0, "prev_close": 370.0, "change_pct": 0.0}
    # Exploding provider collapses to None, doesn't raise.
    assert by_sym["BOOM"]["quote"] is None


def test_original_holding_fields_preserved() -> None:
    out = enrich_holdings(
        [{"id": 7, "symbol": "AAPL", "label": "core", "shares": 10.0, "notes": "n"}],
        records=[],
        quote_fn=lambda s: None,
        now=NOW,
    )
    h = out[0]
    assert h["id"] == 7
    assert h["label"] == "core"
    assert h["shares"] == 10.0
    assert h["notes"] == "n"
    # Enrichment fields added alongside.
    assert {"recent_news_count", "recent_top", "coverage", "quote"} <= set(h)


def test_tolerant_of_malformed_records_and_missing_symbol() -> None:
    records = [
        "not a dict",
        42,
        None,
        # Has a string-symbol field AND a usable timestamp → matches AAPL.
        _rec(symbols=["AAPL"], age_seconds=300),
        # Same symbol but NO timestamp → skipped (can't be placed in-window).
        {"title": None, "candidate_symbols": "AAPL"},
    ]
    out = enrich_holdings(
        [{"symbol": "AAPL"}, {"label": "no-symbol-here"}, "junk"],
        records=records,  # type: ignore[arg-type]
        quote_fn=lambda s: None,
        now=NOW,
    )
    # "junk" holding dropped; two dict holdings survive.
    assert len(out) == 2
    by = {h.get("symbol", ""): h for h in out}
    # Only the timestamped record counts (the no-timestamp one is ignored).
    assert by["AAPL"]["recent_news_count"] == 1
    # Blank-symbol holding: empty-but-shaped enrichment, never raises.
    empty = by[""]
    assert empty["recent_news_count"] == 0
    assert empty["recent_top"] == []
    assert empty["coverage"]["covered"] is False
    assert empty["quote"] is None


def test_deterministic_with_injected_now() -> None:
    records = [_rec(title="AAPL news", symbols=["AAPL"], age_seconds=300, score=0.7)]
    args = dict(records=records, quote_fn=lambda s: {"last": 1.0, "prev_close": 1.0}, now=NOW)
    a = enrich_holdings([{"symbol": "AAPL"}], **args)
    b = enrich_holdings([{"symbol": "AAPL"}], **args)
    assert a == b


# ──────────────────────────────────────────────────────────────────────────────
# Non-finite price rejection (v80 audit fix) — a NaN/inf price from the provider
# must not poison the derived change_pct or leak a NaN into the output.
# ──────────────────────────────────────────────────────────────────────────────


def test_quote_non_finite_prices_are_rejected_not_nan_poisoned() -> None:
    import math

    out = enrich_holdings(
        [{"symbol": "AAPL"}],
        records=[],
        quote_fn=lambda s: {"last": float("nan"), "prev_close": 100.0},
        now=NOW,
    )
    q = out[0]["quote"]
    assert q is not None
    assert q["last"] is None, "a NaN price must coerce to None, not pass through"
    assert q["change_pct"] is None, "no change_pct may be derived from a rejected price"
    # Nothing in the quote may be a non-finite float.
    for v in q.values():
        if isinstance(v, float):
            assert math.isfinite(v)


def test_coerce_float_rejects_non_finite() -> None:
    from catchem.portfolio import _coerce_float

    assert _coerce_float(float("nan")) is None
    assert _coerce_float(float("inf")) is None
    assert _coerce_float(float("-inf")) is None
    # Finite values and tolerant string coercion still work.
    assert _coerce_float("3.14") == 3.14
    assert _coerce_float(2) == 2.0
    assert _coerce_float(None) is None
    assert _coerce_float("not a number") is None
