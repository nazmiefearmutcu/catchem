"""Tests for ``catchem.quant.sentiment_momentum``.

Each test pins one contract guarantee from the module docstring so a
regression there points at exactly one expectation.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from catchem.quant.sentiment_momentum import (
    SentimentBucket,
    SentimentMomentumReport,
    TickerMomentum,
    _clamp,
    _clean_symbols,
    _detect_flip,
    _half_means,
    _parse_ts,
    _safe_float,
    compute_sentiment_momentum,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    published_ts: str,
    *,
    candidate_symbols: list[str] | None = None,
    sentiment_label: str | None = "neutral",
    sentiment_score: float | None = 0.5,
    finance_relevance_score: float = 0.7,
    capture_id: str | None = None,
) -> dict:
    """Build a minimal record dict carrying only the fields we read."""

    return {
        "capture_id": capture_id or f"cap-{published_ts}",
        "published_ts": published_ts,
        "created_at": published_ts,
        "candidate_symbols": list(candidate_symbols or []),
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "finance_relevance_score": finance_relevance_score,
    }


def _pos(ts: str, sym: str = "AAPL", score: float = 0.85) -> dict:
    return _record(
        ts,
        candidate_symbols=[sym],
        sentiment_label="positive",
        sentiment_score=score,
    )


def _neg(ts: str, sym: str = "AAPL", score: float = 0.85) -> dict:
    return _record(
        ts,
        candidate_symbols=[sym],
        sentiment_label="negative",
        sentiment_score=score,
    )


def _neu(ts: str, sym: str = "AAPL", score: float = 0.5) -> dict:
    return _record(
        ts,
        candidate_symbols=[sym],
        sentiment_label="neutral",
        sentiment_score=score,
    )


def _find(report: SentimentMomentumReport, symbol: str) -> TickerMomentum | None:
    for ticker in report.tickers:
        if ticker.symbol == symbol:
            return ticker
    return None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_yields_empty_report() -> None:
    """Empty input ⇒ empty ticker list, params still echoed."""

    report = compute_sentiment_momentum([], bucket_minutes=60, min_mentions=2)
    assert isinstance(report, SentimentMomentumReport)
    assert report.tickers == ()
    assert report.bucket_minutes == 60
    assert report.min_mentions == 2


def test_invalid_bucket_minutes_raises() -> None:
    """Zero / negative widths are nonsensical."""

    with pytest.raises(ValueError):
        compute_sentiment_momentum([], bucket_minutes=0)
    with pytest.raises(ValueError):
        compute_sentiment_momentum([], bucket_minutes=-15)


def test_invalid_min_mentions_raises() -> None:
    """``min_mentions`` of zero would let every ticker through which is nonsense."""

    with pytest.raises(ValueError):
        compute_sentiment_momentum([], min_mentions=0)


def test_invalid_max_tickers_raises() -> None:
    with pytest.raises(ValueError):
        compute_sentiment_momentum([], max_tickers=-1)


def test_records_without_symbols_or_timestamps_skipped() -> None:
    """Records missing usable fields shouldn't crash; they get dropped."""

    bad = [
        _record("not-a-timestamp", candidate_symbols=["AAPL"]),
        _record("2024-01-01T09:00:00Z", candidate_symbols=[]),
        {"capture_id": "naked"},  # no timestamp at all
    ]
    report = compute_sentiment_momentum(bad, min_mentions=1)
    assert report.tickers == ()


def test_max_tickers_zero_short_circuits() -> None:
    """``max_tickers=0`` must return an empty list even with rich input."""

    records = [_pos(f"2024-01-01T0{i}:00:00Z") for i in range(6)]
    report = compute_sentiment_momentum(records, min_mentions=2, max_tickers=0)
    assert report.tickers == ()


# ---------------------------------------------------------------------------
# Direction / flip detection
# ---------------------------------------------------------------------------


def test_all_positive_aapl_is_stable_no_flip() -> None:
    """6 positive records across 3 buckets ⇒ stable, no flip, near-zero momentum."""

    # 4h buckets — these timestamps span 3 distinct buckets.
    records = [
        _pos("2024-01-01T00:30:00Z"),
        _pos("2024-01-01T01:30:00Z"),
        _pos("2024-01-01T04:30:00Z"),
        _pos("2024-01-01T05:30:00Z"),
        _pos("2024-01-01T08:30:00Z"),
        _pos("2024-01-01T09:30:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert len(aapl.buckets) == 3
    # All buckets unanimous-positive ⇒ net=+1.0 across the board.
    assert all(b.net_sentiment == pytest.approx(1.0) for b in aapl.buckets)
    assert aapl.overall_net_sentiment == pytest.approx(1.0)
    assert aapl.momentum == pytest.approx(0.0)
    assert aapl.velocity == pytest.approx(0.0)
    assert aapl.flip_detected is False
    assert aapl.direction == "stable"


def test_aapl_flips_from_positive_to_negative() -> None:
    """3 positive buckets then 3 negative buckets ⇒ flipping_negative + flip_detected."""

    records = [
        # Early positive half (buckets 0..2).
        _pos("2024-01-01T00:30:00Z"),
        _pos("2024-01-01T04:30:00Z"),
        _pos("2024-01-01T08:30:00Z"),
        # Late negative half (buckets 3..5).
        _neg("2024-01-01T12:30:00Z"),
        _neg("2024-01-01T16:30:00Z"),
        _neg("2024-01-01T20:30:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert len(aapl.buckets) == 6
    assert aapl.direction == "flipping_negative"
    assert aapl.flip_detected is True
    # Late half mean = -1, early half mean = +1 ⇒ momentum = -2.
    assert aapl.momentum == pytest.approx(-2.0)
    # Velocity = mean of (0, -2, 0, 0, 0) = -0.4 (only the bucket-3 hop flips).
    assert aapl.velocity == pytest.approx(-2.0 / 5)
    # Overall net = (3 - 3) / 6 = 0.
    assert aapl.overall_net_sentiment == pytest.approx(0.0)


def test_aapl_flips_from_negative_to_positive() -> None:
    """Symmetry check: negative-to-positive should fire ``flipping_positive``."""

    records = [
        _neg("2024-01-01T00:30:00Z"),
        _neg("2024-01-01T04:30:00Z"),
        _neg("2024-01-01T08:30:00Z"),
        _pos("2024-01-01T12:30:00Z"),
        _pos("2024-01-01T16:30:00Z"),
        _pos("2024-01-01T20:30:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert aapl.direction == "flipping_positive"
    assert aapl.flip_detected is True
    assert aapl.momentum == pytest.approx(2.0)


def test_strengthening_positive_when_late_accelerates_upward() -> None:
    """Early half mildly bullish, late half firmly bullish ⇒ strengthening_positive."""

    # Early buckets: 1 positive + 1 negative each ⇒ net = 0.
    # Late buckets:  unanimous positive ⇒ net = +1.
    records = [
        # Early half (2 buckets, mixed).
        _pos("2024-01-01T00:10:00Z"),
        _neg("2024-01-01T00:20:00Z"),
        _pos("2024-01-01T04:10:00Z"),
        _neg("2024-01-01T04:20:00Z"),
        # Late half (2 buckets, all positive).
        _pos("2024-01-01T08:10:00Z"),
        _pos("2024-01-01T08:20:00Z"),
        _pos("2024-01-01T12:10:00Z"),
        _pos("2024-01-01T12:20:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert aapl.direction == "strengthening_positive"
    assert aapl.momentum == pytest.approx(1.0)


def test_strengthening_negative_when_late_accelerates_downward() -> None:
    """Mirror of the previous test."""

    records = [
        _pos("2024-01-01T00:10:00Z"),
        _neg("2024-01-01T00:20:00Z"),
        _pos("2024-01-01T04:10:00Z"),
        _neg("2024-01-01T04:20:00Z"),
        _neg("2024-01-01T08:10:00Z"),
        _neg("2024-01-01T08:20:00Z"),
        _neg("2024-01-01T12:10:00Z"),
        _neg("2024-01-01T12:20:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert aapl.direction == "strengthening_negative"
    assert aapl.momentum == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Filtering / capping / structure
# ---------------------------------------------------------------------------


def test_ticker_below_min_mentions_is_dropped() -> None:
    """A ticker with mentions < ``min_mentions`` must not appear."""

    records = [
        # AAPL meets the floor.
        _pos("2024-01-01T00:00:00Z", sym="AAPL"),
        _pos("2024-01-01T01:00:00Z", sym="AAPL"),
        _pos("2024-01-01T02:00:00Z", sym="AAPL"),
        _pos("2024-01-01T03:00:00Z", sym="AAPL"),
        # MSFT only has 2 mentions ⇒ drop with min=4.
        _pos("2024-01-01T00:00:00Z", sym="MSFT"),
        _pos("2024-01-01T01:00:00Z", sym="MSFT"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=60, min_mentions=4)
    symbols = {t.symbol for t in report.tickers}
    assert "AAPL" in symbols
    assert "MSFT" not in symbols


def test_single_bucket_ticker_has_zero_velocity_and_zero_momentum() -> None:
    """A ticker compressed into one bucket has no trajectory."""

    records = [
        # All 4 records land inside the same 4h bucket [00:00..04:00).
        _pos("2024-01-01T00:10:00Z"),
        _pos("2024-01-01T01:10:00Z"),
        _pos("2024-01-01T02:10:00Z"),
        _pos("2024-01-01T03:10:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert len(aapl.buckets) == 1
    assert aapl.momentum == pytest.approx(0.0)
    assert aapl.velocity == pytest.approx(0.0)
    assert aapl.direction == "stable"
    assert aapl.flip_detected is False


def test_max_tickers_caps_output() -> None:
    """At most ``max_tickers`` rows come back, picked by |momentum| DESC."""

    # 5 tickers, each meeting min_mentions=4. We hand-craft AAPL/MSFT to
    # have the strongest swings so we know which two survive max=2.
    records: list[dict] = []
    # AAPL: 4 negative then 4 positive ⇒ momentum +2 (flip).
    for h in range(4):
        records.append(_neg(f"2024-01-01T0{h}:00:00Z", sym="AAPL"))
    for h in range(4):
        records.append(_pos(f"2024-01-01T1{h}:00:00Z", sym="AAPL"))
    # MSFT: 4 positive then 4 negative ⇒ momentum -2 (flip).
    for h in range(4):
        records.append(_pos(f"2024-01-01T0{h}:00:00Z", sym="MSFT"))
    for h in range(4):
        records.append(_neg(f"2024-01-01T1{h}:00:00Z", sym="MSFT"))
    # Three calm tickers: all neutral ⇒ momentum 0.
    for sym in ("GOOG", "AMZN", "NVDA"):
        for h in range(4):
            records.append(_neu(f"2024-01-01T0{h}:00:00Z", sym=sym, score=0.5))

    report = compute_sentiment_momentum(records, bucket_minutes=60, min_mentions=4, max_tickers=2)
    assert len(report.tickers) == 2
    assert {t.symbol for t in report.tickers} == {"AAPL", "MSFT"}
    # Sort key is abs(momentum) DESC — both are 2.0, tie-broken on symbol asc.
    assert report.tickers[0].symbol == "AAPL"
    assert report.tickers[1].symbol == "MSFT"


def test_buckets_are_sorted_chronologically_per_ticker() -> None:
    """Each ticker's bucket list must be in ascending bucket_start order."""

    # Insert records out-of-order to force the sort path.
    records = [
        _pos("2024-01-01T16:30:00Z"),
        _pos("2024-01-01T00:30:00Z"),
        _pos("2024-01-01T08:30:00Z"),
        _pos("2024-01-01T04:30:00Z"),
        _pos("2024-01-01T12:30:00Z"),
        _pos("2024-01-01T20:30:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    starts = [b.bucket_start for b in aapl.buckets]
    assert starts == sorted(starts)
    assert aapl.last_bucket_start == starts[-1]


# ---------------------------------------------------------------------------
# Multi-ticker, gap-skip, and field correctness
# ---------------------------------------------------------------------------


def test_per_record_mention_counts_each_symbol_once() -> None:
    """A record with [AAPL, MSFT] contributes one mention to each ticker."""

    records = [
        _record(
            f"2024-01-01T0{h}:00:00Z",
            candidate_symbols=["AAPL", "MSFT"],
            sentiment_label="positive",
            sentiment_score=0.8,
        )
        for h in range(4)
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=60, min_mentions=4)
    aapl = _find(report, "AAPL")
    msft = _find(report, "MSFT")
    assert aapl is not None and msft is not None
    assert aapl.mention_count == 4
    assert msft.mention_count == 4


def test_zero_mention_gap_buckets_are_skipped_for_ticker() -> None:
    """Empty buckets between AAPL mentions don't appear in AAPL's trajectory."""

    # AAPL mentioned at t=0h and t=12h with a quiet middle. With 4h buckets
    # there are 3 calendar buckets in between AAPL would NOT show in.
    records = [
        _pos("2024-01-01T00:30:00Z"),
        _pos("2024-01-01T00:45:00Z"),
        _pos("2024-01-01T01:30:00Z"),
        _pos("2024-01-01T01:45:00Z"),
        _pos("2024-01-01T12:30:00Z"),
        _pos("2024-01-01T12:45:00Z"),
        _pos("2024-01-01T13:30:00Z"),
        _pos("2024-01-01T13:45:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    # Two non-empty buckets (00:00 and 12:00) — not 4.
    assert len(aapl.buckets) == 2


def test_bucket_field_counts_match_sentiment_labels() -> None:
    """positive + neutral + negative counts mirror the inputs in each bucket."""

    records = [
        _pos("2024-01-01T00:10:00Z", score=0.9),
        _pos("2024-01-01T00:30:00Z", score=0.8),
        _neu("2024-01-01T00:45:00Z", score=0.5),
        _neg("2024-01-01T00:55:00Z", score=0.7),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert len(aapl.buckets) == 1
    bucket = aapl.buckets[0]
    assert isinstance(bucket, SentimentBucket)
    assert bucket.count == 4
    assert bucket.positive == 2
    assert bucket.neutral == 1
    assert bucket.negative == 1
    # Net = (2 - 1) / 4 = 0.25
    assert bucket.net_sentiment == pytest.approx(0.25)
    # Mean score = (0.9 + 0.8 + 0.5 + 0.7) / 4 = 0.725
    assert bucket.mean_score == pytest.approx(0.725)


def test_records_without_sentiment_label_dont_inflate_net() -> None:
    """Unlabelled mentions contribute to ``count`` but not to net_sentiment."""

    records = [
        _pos("2024-01-01T00:10:00Z"),
        _pos("2024-01-01T00:20:00Z"),
        # Two unlabelled rows.
        _record(
            "2024-01-01T00:30:00Z",
            candidate_symbols=["AAPL"],
            sentiment_label=None,
            sentiment_score=None,
        ),
        _record(
            "2024-01-01T00:40:00Z",
            candidate_symbols=["AAPL"],
            sentiment_label=None,
            sentiment_score=None,
        ),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    bucket = aapl.buckets[0]
    # Net stays at +1.0 — the 2 unlabelled rows widen ``count`` but are
    # divided out of the net_sentiment denominator.
    assert bucket.count == 4
    assert bucket.positive == 2
    assert bucket.neutral == 0
    assert bucket.negative == 0
    assert bucket.net_sentiment == pytest.approx(1.0)


def test_overall_net_sentiment_is_full_window_aggregate() -> None:
    """``overall_net_sentiment`` averages across mentions, not across buckets."""

    # Bucket A (heavy, mostly positive): 7 pos + 1 neg.
    # Bucket B (light, all negative): 1 neg.
    # Per-bucket mean would give (0.75 + -1.0)/2 = -0.125.
    # Mention-weighted should give (7 - 2) / 9 ≈ 0.5556.
    records = []
    for i in range(7):
        records.append(_pos(f"2024-01-01T00:{i + 10}:00Z"))
    records.append(_neg("2024-01-01T00:30:00Z"))
    records.append(_neg("2024-01-01T08:30:00Z"))
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert aapl.overall_net_sentiment == pytest.approx((7 - 2) / 9)


def test_momentum_clamps_to_two() -> None:
    """Defensive: even pathological inputs stay in [-2, +2]."""

    records = [
        _pos("2024-01-01T00:00:00Z"),
        _pos("2024-01-01T01:00:00Z"),
        _neg("2024-01-01T08:00:00Z"),
        _neg("2024-01-01T09:00:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=60, min_mentions=2)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert -2.0 <= aapl.momentum <= 2.0


def test_tickers_sorted_by_abs_momentum_desc() -> None:
    """Top-of-list ticker always has the largest |momentum|."""

    # AAPL: flip ⇒ |mom|=2
    # GOOG: positive plateau ⇒ |mom|=0
    # NVDA: gradual upward ⇒ |mom| in between
    records: list[dict] = []
    for h in range(4):
        records.append(_neg(f"2024-01-01T0{h}:00:00Z", sym="AAPL"))
    for h in range(4):
        records.append(_pos(f"2024-01-01T1{h}:00:00Z", sym="AAPL"))
    for h in range(8):
        ts = f"2024-01-01T0{h}:00:00Z" if h < 10 else f"2024-01-01T{h}:00:00Z"
        records.append(_pos(ts, sym="GOOG"))
    # NVDA: 4 neutral then 4 positive ⇒ |mom|=1
    for h in range(4):
        records.append(_neu(f"2024-01-01T0{h}:00:00Z", sym="NVDA"))
    for h in range(4):
        records.append(_pos(f"2024-01-01T1{h}:00:00Z", sym="NVDA"))

    report = compute_sentiment_momentum(records, bucket_minutes=60, min_mentions=4)
    moms = [abs(t.momentum) for t in report.tickers]
    assert moms == sorted(moms, reverse=True)
    assert report.tickers[0].symbol == "AAPL"


# ---------------------------------------------------------------------------
# Helper-level edge cases (_parse_ts / _safe_float / _clean_symbols / etc.)
# ---------------------------------------------------------------------------


def test_parse_ts_naive_string_is_assumed_utc() -> None:
    """A timestamp with no offset is interpreted as UTC, not local time."""

    parsed = _parse_ts("2024-01-01T09:00:00")
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed.isoformat() == "2024-01-01T09:00:00+00:00"


def test_parse_ts_offset_string_is_normalized_to_utc() -> None:
    """A non-UTC offset is converted to UTC (the else branch of _parse_ts)."""

    parsed = _parse_ts("2024-01-01T12:00:00+03:00")
    assert parsed is not None
    assert parsed.tzinfo is UTC
    # 12:00 at +03:00 is 09:00 UTC.
    assert parsed.isoformat() == "2024-01-01T09:00:00+00:00"


def test_parse_ts_rejects_garbage_and_non_strings() -> None:
    """Unparseable / non-string inputs return None for fall-through."""

    assert _parse_ts("not-a-timestamp") is None
    assert _parse_ts("") is None
    assert _parse_ts(None) is None  # type: ignore[arg-type]
    assert _parse_ts(12345) is None  # type: ignore[arg-type]


def test_safe_float_rejects_bool_nan_inf_and_junk() -> None:
    """Bools, NaN, +/-inf and non-numeric strings all coerce to None."""

    assert _safe_float(True) is None  # bool excluded despite int subclass
    assert _safe_float(False) is None
    assert _safe_float(float("nan")) is None
    assert _safe_float(float("inf")) is None
    assert _safe_float(float("-inf")) is None
    assert _safe_float("junk") is None
    assert _safe_float(None) is None
    # Valid numbers still pass through.
    assert _safe_float("0.5") == pytest.approx(0.5)
    assert _safe_float(3) == pytest.approx(3.0)


def test_clean_symbols_dedupes_uppercases_and_drops_junk() -> None:
    """Mixed-case duplicates collapse; non-strings and blanks are dropped."""

    rec = {"candidate_symbols": ["aapl", "AAPL", " msft ", "", 42, None, "GOOG"]}
    assert _clean_symbols(rec) == ["AAPL", "MSFT", "GOOG"]


def test_clean_symbols_non_list_value_returns_empty() -> None:
    """A scalar (non-list/tuple) candidate_symbols yields an empty list."""

    assert _clean_symbols({"candidate_symbols": "AAPL"}) == []
    assert _clean_symbols({"candidate_symbols": None}) == []
    assert _clean_symbols({}) == []


def test_clamp_pins_to_bounds() -> None:
    """_clamp returns lo / hi / value across the three branches."""

    assert _clamp(-5.0, -2.0, 2.0) == -2.0  # below lo
    assert _clamp(5.0, -2.0, 2.0) == 2.0  # above hi
    assert _clamp(0.3, -2.0, 2.0) == 0.3  # in-band


def test_detect_flip_false_for_single_bucket() -> None:
    """A 1-element net list can't flip (n < 2 guard)."""

    assert _detect_flip([0.9]) is False


def test_detect_flip_false_when_a_half_is_essentially_zero() -> None:
    """If either half-mean is ~0, FP noise must not register as a flip."""

    # Head mean = 0 (cancels), tail strongly negative ⇒ no flip fires.
    assert _detect_flip([1.0, -1.0, 0.0, -1.0]) is False


def test_half_means_single_element_returns_value_twice() -> None:
    """The n==1 branch returns (x, x) so momentum is trivially zero."""

    assert _half_means([0.4]) == (0.4, 0.4)


def test_non_mapping_records_are_skipped() -> None:
    """Junk list members (str / int / None) don't crash the aggregator."""

    records: list = [
        "i am not a record",
        42,
        None,
        _pos("2024-01-01T00:10:00Z"),
        _pos("2024-01-01T00:20:00Z"),
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=2)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert aapl.mention_count == 2


def test_bucket_with_no_labelled_rows_reports_zero_net_and_overall() -> None:
    """Mentions that never carry a sentiment label keep net + overall at 0.0.

    Exercises the ``labelled == 0`` branch in _BucketAgg.net_sentiment and
    the ``total_labelled == 0`` branch for overall_net_sentiment, plus the
    mean_score / mean_relevance zero-divisor guards.
    """

    records = [
        _record(
            f"2024-01-01T00:{m}:00Z",
            candidate_symbols=["AAPL"],
            sentiment_label=None,
            sentiment_score=None,
            finance_relevance_score=float("nan"),
        )
        for m in (10, 20, 30, 40)
    ]
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    bucket = aapl.buckets[0]
    assert bucket.count == 4
    assert bucket.positive == bucket.neutral == bucket.negative == 0
    assert bucket.net_sentiment == pytest.approx(0.0)
    assert bucket.mean_score == pytest.approx(0.0)  # score_n == 0 guard
    assert bucket.mean_relevance == pytest.approx(0.0)  # NaN dropped → n == 0
    assert aapl.overall_net_sentiment == pytest.approx(0.0)


def test_created_at_fallback_when_published_ts_unparseable() -> None:
    """A record with a bad published_ts still buckets via created_at."""

    records = []
    for m in (10, 20, 30, 40):
        rec = _pos("2024-01-01T00:00:00Z")
        rec["published_ts"] = "garbage"
        rec["created_at"] = f"2024-01-01T00:{m}:00Z"
        records.append(rec)
    report = compute_sentiment_momentum(records, bucket_minutes=240, min_mentions=4)
    aapl = _find(report, "AAPL")
    assert aapl is not None
    assert aapl.mention_count == 4


def test_sentiment_momentum_edge_cases(monkeypatch) -> None:
    # 1. _floor_bucket with ts < anchor
    from datetime import datetime

    from catchem.quant.sentiment_momentum import _floor_bucket, _safe_float

    anchor = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    ts = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
    # This should trigger delta_seconds < 0 -> delta_seconds = 0
    res = _floor_bucket(ts, anchor, bucket_minutes=60)
    assert res == anchor

    # Trigger delta_seconds >= 0 branch in _floor_bucket
    ts_after = datetime(2024, 1, 1, 13, 0, tzinfo=UTC)
    res_after = _floor_bucket(ts_after, anchor, bucket_minutes=60)
    assert res_after == datetime(2024, 1, 1, 13, 0, tzinfo=UTC)

    # 2. _detect_flip with head_size < 1
    import math

    def mock_ceil(x):
        return 5

    monkeypatch.setattr(math, "ceil", mock_ceil)

    # We call _detect_flip with nets of length 5
    from catchem.quant.sentiment_momentum import _detect_flip

    assert _detect_flip([1.0, 2.0, 3.0, 4.0, 5.0]) is False

    # 3. _safe_float with string representations of NaN/inf to hit line 233
    assert _safe_float("nan") is None
    assert _safe_float("inf") is None
    assert _safe_float("-inf") is None

    # 4. Trigger delta_seconds < 0 in compute_sentiment_momentum
    class CustomFloat(float):
        def __sub__(self, other):
            return -10.0

    import catchem.quant.sentiment_momentum as sm

    monkeypatch.setattr(sm, "_get_timestamp", lambda dt: CustomFloat(1000.0))

    records = [
        _record("2024-01-01T00:00:00Z", candidate_symbols=["AAPL"]),
        _record("2024-01-01T00:01:00Z", candidate_symbols=["AAPL"]),
    ]
    sm.compute_sentiment_momentum(records, min_mentions=2)
