"""Tests for `catchem.quant.symbol_correlation`.

Each test owns one contract guarantee so a regression there points at
exactly one expectation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from catchem.quant.symbol_correlation import (
    SymbolPair,
    _parse_ts,
    _pearson,
    compute_pairs,
)


_BASE = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def _ts(minutes: int) -> str:
    """ISO Z-suffix timestamp `_BASE + minutes`."""

    return (_BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _rec(minutes: int, symbols: list[str]) -> dict:
    """Minimal record dict — only `published_ts` and `candidate_symbols` are read."""

    return {"published_ts": _ts(minutes), "candidate_symbols": symbols}


# ── _pearson primitive ───────────────────────────────────────────────────


def test_pearson_perfect_positive_is_one() -> None:
    """y = 2x + 1 over a non-flat range collapses to r = +1.0 exactly."""

    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [3.0, 5.0, 7.0, 9.0]
    assert _pearson(xs, ys) == pytest.approx(1.0, abs=1e-9)


def test_pearson_perfect_negative_is_minus_one() -> None:
    """y = -x collapses to r = -1.0 exactly."""

    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [4.0, 3.0, 2.0, 1.0]
    assert _pearson(xs, ys) == pytest.approx(-1.0, abs=1e-9)


def test_pearson_flat_input_returns_zero() -> None:
    """A flat series has zero variance → result is undefined; we return 0.0."""

    assert _pearson([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) == 0.0
    assert _pearson([1.0], [2.0]) == 0.0  # n<2 → 0.0 (not NaN, not exception)


def test_pearson_mismatched_length_returns_zero() -> None:
    """``len(xs) != len(ys)`` is undefined → 0.0 (line 48 guard)."""

    assert _pearson([1.0, 2.0, 3.0], [1.0, 2.0]) == 0.0


def test_pearson_clamps_above_one() -> None:
    """A pair whose raw r overshoots +1 by FP error is clamped to 1.0 (line 61-62).

    ``_pearson`` runs in float64; with these exact perfectly-correlated
    inputs (``ys = a*xs + b``) the covariance/denominator ratio rounds to
    ``1.0000000000000004`` *before* the guard. The clamp must collapse it
    to a flat ``1.0`` so the UI never renders ``1.0000000000000004``.
    """

    xs = [0.232, 0.507, 0.452, 0.412]
    ys = [1.7240000000000002, 3.649, 3.2640000000000002, 2.984]
    r = _pearson(xs, ys)
    assert r == 1.0  # exactly clamped, not 1.0000000000000004
    assert -1.0 <= r <= 1.0


def test_pearson_clamps_below_minus_one() -> None:
    """The negative mirror — raw r of ``-1.0000000000000004`` pins to -1.0 (line 63-64)."""

    xs = [0.232, 0.507, 0.452, 0.412]
    ys = [-1.524, -3.449, -3.064, -2.784]
    r = _pearson(xs, ys)
    assert r == -1.0  # exactly clamped, not -1.0000000000000004
    assert -1.0 <= r <= 1.0


# ── _parse_ts primitive ────────────────────────────────────────────────────


def test_parse_ts_naive_iso_is_assumed_utc() -> None:
    """A naive ISO string (no offset) is stamped UTC, not rejected (line 81-82).

    Unlike ``market_time`` / ``arrival_heatmap``, this signal keeps naive
    timestamps and assumes UTC — pin that contract so a source emitting
    offset-less timestamps still participates in correlation buckets.
    """

    parsed = _parse_ts("2024-01-01T09:00:00")
    assert parsed == datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)


def test_parse_ts_whitespace_only_returns_none() -> None:
    """A blank-after-strip string is unparseable → None (line 73-74)."""

    assert _parse_ts("   ") is None
    assert _parse_ts("") is None
    assert _parse_ts(None) is None  # type: ignore[arg-type]
    assert _parse_ts(12345) is None  # type: ignore[arg-type]


def test_parse_ts_offset_is_normalised_to_utc() -> None:
    """A non-UTC offset is converted to UTC (the ``astimezone`` branch)."""

    assert _parse_ts("2024-01-01T11:00:00+02:00") == datetime(
        2024, 1, 1, 9, 0, tzinfo=timezone.utc
    )


# ── compute_pairs surface ────────────────────────────────────────────────


def test_compute_pairs_empty_input_returns_empty_list() -> None:
    """No records ⇒ no pairs — and crucially no exception."""

    assert compute_pairs([]) == []


def test_compute_pairs_filters_by_min_mentions() -> None:
    """A symbol below min_mentions is dropped before pairing.

    Three records co-mentioning AAPL with two different counterparties:
    MSFT (3 hits, passes default min_mentions=3) and NVDA (1 hit, fails).
    Only AAPL-MSFT should survive.
    """

    records = [
        _rec(0, ["AAPL", "MSFT"]),
        _rec(120, ["AAPL", "MSFT"]),
        _rec(240, ["AAPL", "MSFT", "NVDA"]),
    ]
    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=3, top_n=10)
    pair_keys = {(p.symbol_a, p.symbol_b) for p in pairs}
    # AAPL-MSFT must be present; NVDA pairs must NOT be present.
    assert ("AAPL", "MSFT") in pair_keys
    assert not any("NVDA" in (a, b) for a, b in pair_keys)


def test_compute_pairs_top_n_caps_output() -> None:
    """Output length never exceeds top_n even with many eligible pairs.

    Six symbols mentioned 3× each across 3 buckets ⇒ C(6,2)=15 pairs,
    but top_n=5 must cap.
    """

    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    records: list[dict] = []
    for bucket_idx in range(3):
        for s in syms:
            records.append(_rec(bucket_idx * 60, [s]))
    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=3, top_n=5)
    assert len(pairs) <= 5


def test_compute_pairs_co_moving_symbols_score_high_positive() -> None:
    """Two symbols co-occurring in every bucket get a high positive r.

    AAPL and MSFT appear together in every record across 4 buckets;
    JPM appears only in odd buckets. AAPL-MSFT should outrank pairs
    involving JPM by |r|.
    """

    records: list[dict] = []
    for bucket_idx, mentions in enumerate([3, 5, 2, 4]):
        for _ in range(mentions):
            records.append(_rec(bucket_idx * 60, ["AAPL", "MSFT"]))
        if bucket_idx % 2 == 1:
            records.append(_rec(bucket_idx * 60, ["JPM"]))
    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=2, top_n=10)
    assert pairs, "expected at least one pair given co-movement signal"
    top = pairs[0]
    assert {top.symbol_a, top.symbol_b} == {"AAPL", "MSFT"}
    assert top.pearson_r > 0.95
    # n_buckets reflects 4 distinct bucket keys observed.
    assert top.n_buckets == 4


def test_compute_pairs_returns_sorted_by_abs_r_descending() -> None:
    """The output list must be monotonically non-increasing in |pearson_r|."""

    records: list[dict] = []
    # AAPL+MSFT perfectly correlated (both appear in same buckets, same count).
    for bucket_idx, n in enumerate([4, 1, 6, 2]):
        for _ in range(n):
            records.append(_rec(bucket_idx * 60, ["AAPL", "MSFT"]))
    # NVDA appears with a flat-ish profile (weak correlation candidate).
    for bucket_idx in range(4):
        records.append(_rec(bucket_idx * 60, ["NVDA"]))
    # GOOG anti-correlates with AAPL/MSFT (high when they're low).
    for bucket_idx, n in enumerate([1, 5, 0, 4]):
        for _ in range(n):
            records.append(_rec(bucket_idx * 60, ["GOOG"]))
    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=3, top_n=20)
    abs_rs = [abs(p.pearson_r) for p in pairs]
    assert abs_rs == sorted(abs_rs, reverse=True)


def test_compute_pairs_pearson_r_is_clamped_in_range() -> None:
    """Pearson r must live in [-1, 1] — no FP overflow leaks to the wire."""

    records: list[dict] = []
    # Perfect correlation between BTC and ETH across many buckets.
    for bucket_idx in range(6):
        # Identical counts in every bucket ⇒ ideal r = +1.0.
        for _ in range(bucket_idx + 2):
            records.append(_rec(bucket_idx * 60, ["BTC", "ETH"]))
    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=2, top_n=5)
    assert pairs
    for p in pairs:
        assert -1.0 <= p.pearson_r <= 1.0


# ── Defensive input handling ──────────────────────────────────────────────


def test_compute_pairs_zero_or_negative_bucket_minutes_returns_empty() -> None:
    """``bucket_minutes <= 0`` short-circuits to ``[]`` (rather than throwing).

    The API path passes user-tunable knobs through; a fat-fingered 0
    must produce a graceful empty result, not a ZeroDivisionError deeper
    in the bucket-key arithmetic.

    Pins line 113.
    """

    records = [
        _rec(0, ["AAPL", "MSFT"]),
        _rec(60, ["AAPL", "MSFT"]),
    ]
    assert compute_pairs(records, bucket_minutes=0) == []
    assert compute_pairs(records, bucket_minutes=-5) == []


def test_compute_pairs_skips_unparseable_and_non_string_symbols() -> None:
    """Bad rows must drop silently — every defensive branch on a single record.

    Covers:
      * unparseable ``published_ts`` with no ``created_at`` fallback (continue at line 125);
      * ``candidate_symbols`` that isn't a list (continue at line 129);
      * non-string entries inside ``candidate_symbols`` (continue at line 133);
      * empty-after-strip and duplicate-in-record entries (continue at line 136);
      * ``len(eligible) < 2`` short-circuit (line 152).

    Pins the full defensive funnel against one input batch.
    """

    records: list[dict] = [
        # Bad timestamp + no created_at fallback → row dropped at line 125.
        {"published_ts": "not-a-date", "candidate_symbols": ["AAPL"]},
        # candidate_symbols is a dict, not a list → row dropped at line 129.
        {"published_ts": _ts(0), "candidate_symbols": {"AAPL": 1}},
        # Mixed real + non-string + empty + duplicate symbols across 2+ buckets
        # so AAPL passes min_mentions=3 but no second eligible symbol survives.
        {"published_ts": _ts(0), "candidate_symbols": ["AAPL", None, 7, "", "AAPL"]},
        {"published_ts": _ts(60), "candidate_symbols": ["AAPL", "  ", "AAPL"]},
        {"published_ts": _ts(120), "candidate_symbols": ["AAPL"]},
    ]

    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=3, top_n=10)

    # Only AAPL is eligible — single-symbol universes have no pairs.
    assert pairs == []
