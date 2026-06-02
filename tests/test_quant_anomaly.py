"""Tests for `catchem.quant.anomaly`.

Each test pins one contract from the module docstring so a regression
points at exactly one expectation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from catchem.quant.anomaly import (
    AnomalyReport,
    SentimentShock,
    SymbolBurst,
    VolumeAnomaly,
    detect_anomalies,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _ts(offset_minutes: float) -> str:
    return (_BASE + timedelta(minutes=offset_minutes)).replace(
        microsecond=0
    ).isoformat()


def _record(
    offset_minutes: float,
    *,
    capture_id: str | None = None,
    candidate_symbols: list[str] | None = None,
    sentiment_label: str | None = None,
) -> dict:
    return {
        "capture_id": capture_id or f"cap-{offset_minutes}",
        "published_ts": _ts(offset_minutes),
        "created_at": _ts(offset_minutes),
        "candidate_symbols": list(candidate_symbols or []),
        "sentiment_label": sentiment_label,
    }


def _records_in_bucket(
    bucket_index: int,
    count: int,
    *,
    bucket_minutes: int = 30,
    sentiment_label: str | None = None,
    candidate_symbols: list[str] | None = None,
    capture_prefix: str = "rec",
) -> list[dict]:
    """Produce ``count`` evenly-spaced records inside one bucket."""

    out: list[dict] = []
    bucket_start = bucket_index * bucket_minutes
    for i in range(count):
        offset = bucket_start + (i * (bucket_minutes / max(count, 1))) * 0.5
        out.append(
            _record(
                offset,
                capture_id=f"{capture_prefix}-b{bucket_index}-{i}",
                sentiment_label=sentiment_label,
                candidate_symbols=candidate_symbols,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_yields_empty_report() -> None:
    """No records => all axes empty, params echoed."""

    report = detect_anomalies([], bucket_minutes=30, window_buckets=12, z_threshold=2.0)
    assert isinstance(report, AnomalyReport)
    assert report.volume_anomalies == ()
    assert report.sentiment_shocks == ()
    assert report.symbol_bursts == ()
    assert report.bucket_minutes == 30
    assert report.window_buckets == 12
    assert report.z_threshold == 2.0


def test_invalid_bucket_minutes_raises() -> None:
    with pytest.raises(ValueError):
        detect_anomalies([], bucket_minutes=0)


def test_invalid_window_buckets_raises() -> None:
    with pytest.raises(ValueError):
        detect_anomalies([], window_buckets=0)


def test_single_bucket_no_anomalies() -> None:
    """One bucket of data => no priors => no anomalies on any axis."""

    records = _records_in_bucket(
        0, 50, candidate_symbols=["AAPL"], sentiment_label="positive"
    )
    report = detect_anomalies(records)
    assert report.volume_anomalies == ()
    assert report.sentiment_shocks == ()
    assert report.symbol_bursts == ()


def test_insufficient_history_no_anomalies() -> None:
    """Only 2 prior buckets — below the 3-bucket minimum — must stay silent."""

    records: list[dict] = []
    for b in range(2):
        records += _records_in_bucket(b, 5, candidate_symbols=["AAPL"])
    # Last bucket is a huge spike but only 2 priors exist.
    records += _records_in_bucket(2, 50, candidate_symbols=["AAPL"])
    report = detect_anomalies(records)
    assert report.volume_anomalies == ()
    assert report.symbol_bursts == ()


# ---------------------------------------------------------------------------
# Volume anomalies
# ---------------------------------------------------------------------------


def test_uniform_volume_yields_no_volume_anomaly() -> None:
    """Flat 5/bucket x 8 buckets has 0 variance => z=0 => no anomaly."""

    records: list[dict] = []
    for b in range(8):
        records += _records_in_bucket(b, 5)
    report = detect_anomalies(records)
    assert report.volume_anomalies == ()


def test_sudden_volume_spike_fires_high_severity() -> None:
    """~5/bucket noisy baseline, last bucket explodes to 50 => high severity.

    We jitter the prior counts slightly so the sample std is non-zero —
    a perfectly flat baseline collapses to z=0 by design (see module
    docstring), which is a separate test (``test_uniform_volume_...``).
    """

    records: list[dict] = []
    prior_counts = [4, 5, 6, 5, 4, 6, 5, 5]
    for b, n in enumerate(prior_counts):
        records += _records_in_bucket(b, n)
    records += _records_in_bucket(8, 50)
    report = detect_anomalies(records)
    assert len(report.volume_anomalies) == 1
    anomaly = report.volume_anomalies[0]
    assert isinstance(anomaly, VolumeAnomaly)
    assert anomaly.observed == 50
    assert anomaly.z_score > 4.0
    assert anomaly.severity == "high"
    # Chronological ordering = single entry trivially satisfies it.
    assert anomaly.bucket_start <= anomaly.bucket_end


def test_volume_anomaly_severity_ladder() -> None:
    """|z| 2-3 = low, 3-4 = medium, >=4 = high."""

    from catchem.quant.anomaly import _severity  # type: ignore[attr-defined]

    assert _severity(2.0) == "low"
    assert _severity(2.9) == "low"
    assert _severity(3.0) == "medium"
    assert _severity(3.9) == "medium"
    assert _severity(4.0) == "high"
    assert _severity(7.5) == "high"
    assert _severity(-4.5) == "high"


# ---------------------------------------------------------------------------
# Sentiment shocks
# ---------------------------------------------------------------------------


def test_sentiment_flip_to_deep_negative_fires_bearish_shock() -> None:
    """Sustained mildly-positive 8 buckets, then deep negative => bearish_shock.

    Vary the positive/neutral mix per baseline bucket so the rolling
    net-sentiment series has non-zero variance (a flat baseline collapses
    sample-std to 0 by design — see module docstring).
    """

    records: list[dict] = []
    # Net per bucket = (pos - 0) / (pos + neu): 0.83, 0.6, 0.75, 0.8, 0.67, 0.71, 0.78, 0.83.
    mix = [(5, 1), (3, 2), (3, 1), (4, 1), (4, 2), (5, 2), (7, 2), (5, 1)]
    for b, (pos, neu) in enumerate(mix):
        records += _records_in_bucket(
            b, pos, sentiment_label="positive", capture_prefix=f"pos-{b}"
        )
        records += _records_in_bucket(
            b, neu, sentiment_label="neutral", capture_prefix=f"neu-{b}"
        )
    # Bucket 8: all negative.
    records += _records_in_bucket(8, 5, sentiment_label="negative")
    report = detect_anomalies(records)
    assert len(report.sentiment_shocks) == 1
    shock = report.sentiment_shocks[0]
    assert isinstance(shock, SentimentShock)
    assert shock.observed_net == pytest.approx(-1.0)
    assert shock.z_score < -2.0
    assert shock.direction == "bearish_shock"


def test_sentiment_buckets_with_no_labels_skip_baseline() -> None:
    """Unlabelled bucket doesn't update the rolling sentiment baseline."""

    records: list[dict] = []
    mix = [(5, 1), (3, 2), (3, 1), (4, 1), (4, 2), (5, 2), (7, 2), (5, 1)]
    for b, (pos, neu) in enumerate(mix):
        records += _records_in_bucket(
            b, pos, sentiment_label="positive", capture_prefix=f"pos-{b}"
        )
        records += _records_in_bucket(
            b, neu, sentiment_label="neutral", capture_prefix=f"neu-{b}"
        )
    # Bucket 8: unlabelled — should be skipped silently.
    records += _records_in_bucket(8, 4, sentiment_label=None)
    # Bucket 9: deep negative — should still trip vs. the baseline that
    # bucket 8 did NOT pollute.
    records += _records_in_bucket(9, 4, sentiment_label="negative")
    report = detect_anomalies(records)
    assert len(report.sentiment_shocks) == 1
    assert report.sentiment_shocks[0].direction == "bearish_shock"


# ---------------------------------------------------------------------------
# Symbol bursts
# ---------------------------------------------------------------------------


def test_symbol_burst_fires_for_aapl() -> None:
    """AAPL once/bucket x 10 buckets, then 8 in one bucket => fires."""

    records: list[dict] = []
    for b in range(10):
        records += _records_in_bucket(
            b, 1, candidate_symbols=["AAPL"], capture_prefix=f"calm-{b}"
        )
    # Bucket 10: 8 AAPL mentions.
    records += _records_in_bucket(
        10, 8, candidate_symbols=["AAPL"], capture_prefix="spike"
    )
    report = detect_anomalies(records)
    aapl_bursts = [b for b in report.symbol_bursts if b.symbol == "AAPL"]
    assert len(aapl_bursts) == 1
    burst = aapl_bursts[0]
    assert isinstance(burst, SymbolBurst)
    assert burst.observed == 8
    assert burst.z_score >= 2.0
    assert burst.rolling_mean == pytest.approx(1.0, abs=0.01)


def test_symbol_burst_sample_capture_ids_capped_at_3() -> None:
    """A 10-mention burst yields at most 3 sample_capture_ids."""

    records: list[dict] = []
    for b in range(10):
        records += _records_in_bucket(
            b, 1, candidate_symbols=["TSLA"], capture_prefix=f"calm-{b}"
        )
    records += _records_in_bucket(
        10, 10, candidate_symbols=["TSLA"], capture_prefix="spike"
    )
    report = detect_anomalies(records)
    tsla = [b for b in report.symbol_bursts if b.symbol == "TSLA"]
    assert len(tsla) == 1
    assert len(tsla[0].sample_capture_ids) == 3
    # All sample ids are real capture_ids from the burst bucket.
    for sid in tsla[0].sample_capture_ids:
        assert sid.startswith("spike-b10-")


def test_symbol_burst_requires_observed_at_least_2() -> None:
    """A single mention can't be a burst even if rolling_mean=0."""

    records: list[dict] = []
    # 10 buckets with NO mentions of FOO (just structural records on AAPL).
    for b in range(10):
        records += _records_in_bucket(b, 3, candidate_symbols=["AAPL"])
    # Bucket 10: exactly one FOO mention.
    records += _records_in_bucket(
        10, 1, candidate_symbols=["FOO"], capture_prefix="foo"
    )
    report = detect_anomalies(records)
    assert not any(b.symbol == "FOO" for b in report.symbol_bursts)


def test_symbol_bursts_sorted_by_z_desc_and_topn_respected() -> None:
    """Multiple symbols burst; ranked by z DESC and truncated to top_n."""

    records: list[dict] = []
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    # Build 10 buckets where each symbol gets 1 mention/bucket.
    for b in range(10):
        for sym in symbols:
            records += _records_in_bucket(
                b, 1, candidate_symbols=[sym], capture_prefix=f"calm-{sym}-{b}"
            )
    # Bucket 10: vary spike intensity per symbol.
    bursts = {"AAA": 12, "BBB": 6, "CCC": 4, "DDD": 3}
    for sym, n in bursts.items():
        records += _records_in_bucket(
            10, n, candidate_symbols=[sym], capture_prefix=f"spike-{sym}"
        )
    report = detect_anomalies(records, top_n_symbols=2)
    assert len(report.symbol_bursts) == 2
    # Sorted DESC by z_score.
    zs = [b.z_score for b in report.symbol_bursts]
    assert zs == sorted(zs, reverse=True)
    assert report.symbol_bursts[0].symbol == "AAA"
    assert report.symbol_bursts[1].symbol == "BBB"


# ---------------------------------------------------------------------------
# z_threshold sensitivity
# ---------------------------------------------------------------------------


def test_high_z_threshold_suppresses_anomalies() -> None:
    """The same spike that fires at z=2 must NOT fire at z=20."""

    records: list[dict] = []
    prior_counts = [4, 5, 6, 5, 4, 6, 5, 5]
    for b, n in enumerate(prior_counts):
        records += _records_in_bucket(b, n)
    records += _records_in_bucket(8, 50)

    loose = detect_anomalies(records, z_threshold=2.0)
    strict = detect_anomalies(records, z_threshold=200.0)
    assert len(loose.volume_anomalies) >= 1
    assert strict.volume_anomalies == ()


def test_low_z_threshold_yields_more_anomalies() -> None:
    """Same data, lower threshold flags at least as many anomalies."""

    records: list[dict] = []
    # Mild ramp: 4,5,4,5,4,5,4,5, then a moderate spike to 12.
    counts = [4, 5, 4, 5, 4, 5, 4, 5, 12]
    for b, n in enumerate(counts):
        records += _records_in_bucket(b, n)
    loose = detect_anomalies(records, z_threshold=1.0)
    strict = detect_anomalies(records, z_threshold=2.5)
    assert len(loose.volume_anomalies) >= len(strict.volume_anomalies)


# ---------------------------------------------------------------------------
# Bucket boundaries
# ---------------------------------------------------------------------------


def test_records_grouped_into_correct_bucket_widths() -> None:
    """A bucket_end - bucket_start == bucket_minutes (ISO arithmetic)."""

    records: list[dict] = []
    prior_counts = [4, 5, 6, 5, 4, 6, 5, 5, 4]
    for b, n in enumerate(prior_counts):
        records += _records_in_bucket(b, n, bucket_minutes=15)
    records += _records_in_bucket(9, 60, bucket_minutes=15)
    report = detect_anomalies(records, bucket_minutes=15)
    assert len(report.volume_anomalies) == 1
    a = report.volume_anomalies[0]
    start = datetime.fromisoformat(a.bucket_start)
    end = datetime.fromisoformat(a.bucket_end)
    assert (end - start) == timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Timestamp parsing (_parse_ts / _record_timestamp)
# ---------------------------------------------------------------------------


def test_parse_ts_rejects_non_string_and_empty() -> None:
    """Non-str or empty-str timestamps parse to None."""

    from catchem.quant.anomaly import _parse_ts  # type: ignore[attr-defined]

    assert _parse_ts(None) is None
    assert _parse_ts(123) is None
    assert _parse_ts("") is None
    assert _parse_ts("   ") is None


def test_parse_ts_handles_zulu_suffix() -> None:
    """A trailing ``Z`` is treated as +00:00 UTC."""

    from catchem.quant.anomaly import _parse_ts  # type: ignore[attr-defined]

    parsed = _parse_ts("2026-01-01T12:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_parse_ts_rejects_malformed_iso() -> None:
    """Unparseable ISO strings return None instead of raising."""

    from catchem.quant.anomaly import _parse_ts  # type: ignore[attr-defined]

    assert _parse_ts("not-a-timestamp") is None
    assert _parse_ts("2026-13-99T99:99:99") is None


def test_parse_ts_naive_assumed_utc() -> None:
    """A naive (tz-less) ISO timestamp is stamped as UTC."""

    from catchem.quant.anomaly import _parse_ts  # type: ignore[attr-defined]

    parsed = _parse_ts("2026-01-01T12:00:00")
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed.hour == 12


def test_parse_ts_offset_converted_to_utc() -> None:
    """A tz-aware non-UTC offset is normalized to UTC."""

    from catchem.quant.anomaly import _parse_ts  # type: ignore[attr-defined]

    parsed = _parse_ts("2026-01-01T12:00:00+02:00")
    assert parsed is not None
    assert parsed.tzinfo == UTC
    # 12:00 at +02:00 == 10:00 UTC.
    assert parsed.hour == 10


def test_record_falls_back_to_created_at() -> None:
    """When published_ts is unusable, created_at is used."""

    from catchem.quant.anomaly import _record_timestamp  # type: ignore[attr-defined]

    rec = {"published_ts": None, "created_at": "2026-01-01T12:00:00Z"}
    ts = _record_timestamp(rec)
    assert ts == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_records_without_any_timestamp_are_dropped() -> None:
    """Records with no parseable timestamp are silently skipped.

    A lone undated record among an otherwise-empty stream yields an empty
    report (exercises the per-record ``ts is None: continue`` guard).
    """

    records = [
        {"capture_id": "x", "published_ts": None, "created_at": "garbage"},
        {"capture_id": "y", "published_ts": "", "created_at": ""},
    ]
    report = detect_anomalies(records)
    assert report.volume_anomalies == ()
    assert report.sentiment_shocks == ()
    assert report.symbol_bursts == ()


def test_undated_records_dropped_but_dated_ones_kept() -> None:
    """Mixing undated rows into a real spike doesn't suppress the anomaly."""

    records: list[dict] = []
    prior_counts = [4, 5, 6, 5, 4, 6, 5, 5]
    for b, n in enumerate(prior_counts):
        records += _records_in_bucket(b, n)
    records += _records_in_bucket(8, 50)
    # Inject undated noise that must be dropped, not bucketed.
    records.append({"capture_id": "nodate", "published_ts": "nope", "created_at": None})
    report = detect_anomalies(records)
    assert len(report.volume_anomalies) == 1
    assert report.volume_anomalies[0].observed == 50


# ---------------------------------------------------------------------------
# Sentiment direction classifier (_direction)
# ---------------------------------------------------------------------------


def test_direction_bullish_requires_positive_z_and_level() -> None:
    """Positive z above the net-level threshold => bullish_shock."""

    from catchem.quant.anomaly import _direction  # type: ignore[attr-defined]

    assert _direction(3.0, 0.5) == "bullish_shock"
    # Positive z but net below threshold => neutral, not bullish.
    assert _direction(3.0, 0.10) == "neutral"


def test_direction_bearish_requires_negative_z_and_level() -> None:
    from catchem.quant.anomaly import _direction  # type: ignore[attr-defined]

    assert _direction(-3.0, -0.5) == "bearish_shock"
    assert _direction(-3.0, -0.10) == "neutral"


def test_direction_neutral_when_signs_disagree() -> None:
    """A positive z with a deeply-negative net is neutral (sign mismatch)."""

    from catchem.quant.anomaly import _direction  # type: ignore[attr-defined]

    assert _direction(3.0, -0.9) == "neutral"
    assert _direction(-3.0, 0.9) == "neutral"
    assert _direction(0.0, 0.0) == "neutral"


def test_sentiment_flip_to_strong_positive_fires_bullish_shock() -> None:
    """A mildly-negative baseline then a strong-positive bucket => bullish_shock.

    Mirrors the bearish test but inverts polarity so the bullish branch of
    ``_direction`` is exercised end-to-end.
    """

    records: list[dict] = []
    # Net per bucket is mildly negative with non-zero variance.
    mix = [(5, 1), (3, 2), (3, 1), (4, 1), (4, 2), (5, 2), (7, 2), (5, 1)]
    for b, (neg, neu) in enumerate(mix):
        records += _records_in_bucket(
            b, neg, sentiment_label="negative", capture_prefix=f"neg-{b}"
        )
        records += _records_in_bucket(
            b, neu, sentiment_label="neutral", capture_prefix=f"neu-{b}"
        )
    # Bucket 8: all positive => net = +1.0.
    records += _records_in_bucket(8, 5, sentiment_label="positive")
    report = detect_anomalies(records)
    assert len(report.sentiment_shocks) == 1
    shock = report.sentiment_shocks[0]
    assert shock.observed_net == pytest.approx(1.0)
    assert shock.z_score > 2.0
    assert shock.direction == "bullish_shock"


# ---------------------------------------------------------------------------
# Rolling stats edge (_rolling_stats) + symbol extraction (_symbols_in)
# ---------------------------------------------------------------------------


def test_rolling_stats_below_minimum_returns_none() -> None:
    from catchem.quant.anomaly import _rolling_stats  # type: ignore[attr-defined]

    assert _rolling_stats([]) is None
    assert _rolling_stats([1.0, 2.0]) is None


def test_rolling_stats_flat_window_clamps_std_to_zero() -> None:
    """A flat window yields a real mean and a clamped-to-zero std."""

    from catchem.quant.anomaly import _rolling_stats  # type: ignore[attr-defined]

    stats = _rolling_stats([5.0, 5.0, 5.0, 5.0])
    assert stats is not None
    mean, std = stats
    assert mean == pytest.approx(5.0)
    assert std == 0.0


def test_symbols_in_rejects_non_collection() -> None:
    """A scalar/str ``candidate_symbols`` yields an empty symbol list."""

    from catchem.quant.anomaly import _symbols_in  # type: ignore[attr-defined]

    assert _symbols_in({"candidate_symbols": "AAPL"}) == []
    assert _symbols_in({"candidate_symbols": 123}) == []
    assert _symbols_in({"candidate_symbols": None}) == []


def test_symbols_in_drops_non_strings_blanks_and_dupes() -> None:
    """Only clean, unique, non-empty string symbols survive (order kept)."""

    from catchem.quant.anomaly import _symbols_in  # type: ignore[attr-defined]

    rec = {"candidate_symbols": ["AAPL", 7, None, "  ", "AAPL", " MSFT "]}
    assert _symbols_in(rec) == ["AAPL", "MSFT"]


def test_mean_std_statistics_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import statistics
    from catchem.quant.anomaly import _rolling_stats

    def mock_stdev(*args, **kwargs):
        raise statistics.StatisticsError("mocked stdev error")

    monkeypatch.setattr(statistics, "stdev", mock_stdev)

    res = _rolling_stats([1.0, 2.0, 3.0])
    assert res is not None
    mean, std = res
    assert mean == pytest.approx(2.0)
    assert std == 0.0

