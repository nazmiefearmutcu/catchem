"""Tests for ``catchem.quant.news_velocity``.

Each test pins one contract from the module docstring so a regression
points at exactly one expectation.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from catchem.quant.news_velocity import (
    VelocityReport,
    _classify_regime,
    _parse_ts,
    compute_velocity,
)


_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _ts(offset_seconds: float) -> str:
    return (_BASE + timedelta(seconds=offset_seconds)).replace(
        microsecond=0
    ).isoformat()


def _record(offset_seconds: float, *, capture_id: str | None = None) -> dict:
    return {
        "capture_id": capture_id or f"cap-{int(offset_seconds)}",
        "published_ts": _ts(offset_seconds),
        "created_at": _ts(offset_seconds),
    }


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_yields_all_zero_report() -> None:
    report = compute_velocity([], bucket_minutes=5, window_minutes=60)
    assert isinstance(report, VelocityReport)
    assert report.current_rate_per_min == 0.0
    assert report.ema_fast == 0.0
    assert report.ema_slow == 0.0
    assert report.baseline_rate == 0.0
    assert report.baseline_std == 0.0
    assert report.acceleration_z == 0.0
    assert report.regime == "calm"
    assert report.samples == 0
    assert report.bucket_minutes == 5
    assert report.window_minutes == 60


def test_invalid_bucket_minutes_raises() -> None:
    with pytest.raises(ValueError):
        compute_velocity([], bucket_minutes=0)


def test_invalid_window_minutes_raises() -> None:
    with pytest.raises(ValueError):
        compute_velocity([], window_minutes=0)


def test_records_with_only_bad_timestamps_yield_zero_report() -> None:
    bad = [
        {"published_ts": None, "created_at": None},
        {"published_ts": "not-a-date", "created_at": ""},
        {"published_ts": 12345},  # non-string
    ]
    report = compute_velocity(bad, bucket_minutes=5, window_minutes=60)
    assert report.regime == "calm"
    assert report.samples == 0
    assert report.current_rate_per_min == 0.0


# ---------------------------------------------------------------------------
# Steady state
# ---------------------------------------------------------------------------


def test_steady_arrival_is_calm() -> None:
    """A flat stream — current ≈ median, ``|accel| < 1`` → regime=='calm'.

    A small lead-in tail of zero buckets gets backfilled (window_start
    floors before the earliest record) so stdev stays > 0, but the
    z-score of the latest bucket vs the median should still land
    inside the calm band ``|z| < 1``.
    """

    # 12 buckets * 5 min = 60 min; 4 records per bucket = steady 0.8/min.
    records: list[dict] = []
    for bucket in range(12):
        for i in range(4):
            secs = bucket * 5 * 60 + i * 60
            records.append(_record(secs, capture_id=f"b{bucket}-{i}"))

    report = compute_velocity(records, bucket_minutes=5, window_minutes=60)

    assert report.current_rate_per_min == pytest.approx(0.8)
    assert report.baseline_rate == pytest.approx(0.8)
    # Current matches the median, so accel z-score is ~0 even though
    # the lead-in zero bucket lifts stdev off zero.
    assert abs(report.acceleration_z) < 1.0
    assert report.regime == "calm"
    assert report.samples == 48
    # EMAs converge toward the steady rate — fast catches up faster.
    assert report.ema_fast > report.ema_slow


# ---------------------------------------------------------------------------
# Burst regime
# ---------------------------------------------------------------------------


def test_recent_burst_triggers_burst_regime() -> None:
    """Quiet 55 min then a heavy final bucket → acceleration_z >= 2."""

    records: list[dict] = []
    # 11 buckets with 1 record each — quiet baseline.
    for bucket in range(11):
        records.append(_record(bucket * 5 * 60, capture_id=f"q{bucket}"))
    # Final bucket (index 11) has 40 records.
    final_bucket_start = 11 * 5 * 60
    for i in range(40):
        records.append(_record(final_bucket_start + i, capture_id=f"burst-{i}"))

    report = compute_velocity(records, bucket_minutes=5, window_minutes=60)

    # Final bucket rate = 40 / 5 = 8 rec/min.
    assert report.current_rate_per_min == pytest.approx(8.0)
    # Median baseline pinned to the long quiet tail (0.2 rec/min).
    assert report.baseline_rate == pytest.approx(0.2)
    assert report.baseline_std > 0.0
    assert report.acceleration_z >= 2.0
    assert report.regime == "burst"
    # ema_fast catches the spike faster than ema_slow.
    assert report.ema_fast > report.ema_slow


# ---------------------------------------------------------------------------
# Quiet regime
# ---------------------------------------------------------------------------


def test_recent_quiet_after_busy_baseline_triggers_quiet_regime() -> None:
    """Busy 55 min then a near-empty final bucket → acceleration_z <= -1."""

    records: list[dict] = []
    # 11 busy buckets with 20 records each.
    for bucket in range(11):
        for i in range(20):
            secs = bucket * 5 * 60 + i * 2
            records.append(_record(secs, capture_id=f"busy-{bucket}-{i}"))
    # Final bucket: just one record so it isn't trimmed entirely.
    final_bucket_start = 11 * 5 * 60
    records.append(_record(final_bucket_start, capture_id="lone"))

    report = compute_velocity(records, bucket_minutes=5, window_minutes=60)

    # Final bucket rate = 1 / 5 = 0.2 rec/min.
    assert report.current_rate_per_min == pytest.approx(0.2)
    # Baseline anchored at the busy median (4 rec/min).
    assert report.baseline_rate == pytest.approx(4.0)
    assert report.acceleration_z <= -1.0
    assert report.regime == "quiet"


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_records_outside_window_are_ignored() -> None:
    """A record from 2 hours before ``latest`` must not change the rate."""

    records: list[dict] = []
    # 4 buckets * 4 records starting near _BASE.
    for bucket in range(4):
        for i in range(4):
            secs = bucket * 5 * 60 + i * 30
            records.append(_record(secs, capture_id=f"recent-{bucket}-{i}"))
    # Stale record 2 hours before any of these — outside a 60-min window.
    records.append(_record(-2 * 3600, capture_id="stale"))

    report = compute_velocity(records, bucket_minutes=5, window_minutes=60)

    # Stale record must not show up in `samples`.
    assert report.samples == 16
    assert report.current_rate_per_min > 0.0


def test_published_ts_falls_back_to_created_at_when_invalid() -> None:
    """``_record_timestamp`` short-circuits ``published_ts``→``created_at``
    when the first is unparseable (line 103-104 / 88+94 in ``_parse_ts``).

    Also exercises the naive-ISO branch (``parsed.tzinfo is None``) and the
    tz-aware branch in the same call by mixing both shapes.

    Pins the fallback contract — a record with a bogus ``published_ts``
    but a real ``created_at`` must still be counted, otherwise the
    velocity signal silently undercounts arrivals from sources that
    don't fill ``published_ts``.
    """

    records: list[dict] = [
        # published_ts bogus → fall through to created_at (naive ISO → UTC).
        {
            "published_ts": "garbage",
            "created_at": _ts(0).replace("+00:00", ""),  # naive ISO
        },
        # published_ts as numeric → _parse_ts returns None → created_at picks up.
        {"published_ts": 12345, "created_at": _ts(60)},
        # Whitespace-only created_at (line 86): both fall through, row dropped.
        {"published_ts": None, "created_at": "   "},
        # Real published_ts with tz-aware ISO (line 96 ``astimezone`` branch).
        {"published_ts": _ts(120), "created_at": None},
    ]

    report = compute_velocity(records, bucket_minutes=5, window_minutes=60)

    # Three of four records carry a usable timestamp; the blank-string one drops.
    assert report.samples == 3


# ---------------------------------------------------------------------------
# _classify_regime — every band + the NaN/inf guard (line 64-65, 68-69)
# ---------------------------------------------------------------------------


def test_classify_regime_nan_and_inf_fall_back_to_calm() -> None:
    """A non-finite z-score must never escape the classifier as a number.

    Pins the ``math.isnan(z) or math.isinf(z)`` guard at line 64-65 — a
    degenerate baseline could in principle produce inf, and the regime
    must stay ``calm`` rather than surfacing a garbage label.
    """

    assert _classify_regime(float("nan")) == "calm"
    assert _classify_regime(float("inf")) == "calm"
    assert _classify_regime(float("-inf")) == "calm"


def test_classify_regime_active_band_is_inclusive_of_one() -> None:
    """``_REGIME_ACTIVE <= z < _REGIME_BURST`` maps to ``active`` (line 68-69).

    No ``compute_velocity`` fixture lands cleanly in the active band, so
    pin the boundary directly: z=1.0 is active, z just under 2.0 is still
    active, and z=2.0 tips into burst.
    """

    assert _classify_regime(1.0) == "active"
    assert _classify_regime(1.999) == "active"
    assert _classify_regime(2.0) == "burst"


def test_classify_regime_quiet_and_calm_boundaries() -> None:
    """z<=-1 is quiet; the open interval (-1, 1) is calm (line 70-72)."""

    assert _classify_regime(-1.0) == "quiet"
    assert _classify_regime(-0.999) == "calm"
    assert _classify_regime(0.0) == "calm"


# ---------------------------------------------------------------------------
# _parse_ts — Z-suffix normalisation branch (line 87-88)
# ---------------------------------------------------------------------------


def test_parse_ts_z_suffix_is_normalised_to_utc_offset() -> None:
    """A trailing ``Z`` is rewritten to ``+00:00`` before parsing (line 87-88)."""

    parsed = _parse_ts("2026-01-01T12:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_ts_offset_timestamp_is_converted_to_utc() -> None:
    """A non-UTC offset is normalised to UTC (line 96 ``astimezone`` branch)."""

    # 12:00 at +02:00 == 10:00 UTC.
    parsed = _parse_ts("2026-01-01T12:00:00+02:00")
    assert parsed == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# compute_velocity — window-empty + single-bucket no-variance paths
# ---------------------------------------------------------------------------


def _at(epoch_seconds: int) -> str:
    """ISO timestamp at an absolute epoch second (for bucket-boundary control)."""

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def test_single_bucket_sequence_has_zero_stdev_and_zero_accel() -> None:
    """A lone arrival whose window stays inside one bucket → len(sequence)==1.

    Placing the only record 120 s into a 5-min (300 s) bucket with a 1-min
    window keeps ``window_start`` in the *same* bucket, so the rate
    sequence has length 1. That drives the ``len(sequence) < 2`` branch
    (stdev stays 0.0, line 208-209) *and* the ``baseline_std > 0.0`` being
    false → ``accel = 0.0`` (line 213-216). A single arrival must never
    divide by zero or invent acceleration.
    """

    base = 300 * 100  # an exact 5-min bucket boundary in epoch seconds
    report = compute_velocity(
        [{"published_ts": _at(base + 120)}], bucket_minutes=5, window_minutes=1
    )
    assert report.samples == 1
    assert report.current_rate_per_min == pytest.approx(0.2)  # 1 / 5 min
    assert report.baseline_rate == pytest.approx(0.2)
    assert report.baseline_std == 0.0
    assert report.acceleration_z == 0.0
    assert report.regime == "calm"
    # One bucket seeds both EMAs at the same value — they stay equal.
    assert report.ema_fast == report.ema_slow == pytest.approx(0.2)


def test_lead_in_zero_buckets_lift_stdev_above_zero() -> None:
    """A wide window backfills zero buckets so stdev > 0 even for one record.

    The complement of the single-bucket case: the same lone record under a
    60-min window floors ``window_start`` many buckets earlier, so the
    sequence is padded with leading zeros, stdev > 0, and the lone
    non-zero final bucket reads as acceleration. Pins the ``len>=2`` /
    ``baseline_std > 0`` accel branch (line 213-214).
    """

    report = compute_velocity([_record(0)], bucket_minutes=5, window_minutes=60)
    assert report.samples == 1
    assert report.baseline_std > 0.0
    assert report.acceleration_z != 0.0


def test_narrow_window_keeps_only_latest_bucket() -> None:
    """A 1-min window over records spread across 90 min keeps just the anchor.

    ``window_start = latest - window`` so the latest timestamp always sits
    in-window; a tight window prunes everything older, leaving a single
    bucket. Proves the window-filter math without relying on the
    defensively-dead empty-``in_window`` guard.
    """

    records = [_record(-90 * 60), _record(-90 * 60 + 5), _record(0)]
    report = compute_velocity(records, bucket_minutes=1, window_minutes=1)
    assert report.samples == 1
    assert report.current_rate_per_min == pytest.approx(1.0)  # 1 / 1 min


def test_compute_velocity_burst_label_matches_helper() -> None:
    """The high-level burst report's z-score maps through the helper cleanly.

    Cross-checks that ``compute_velocity`` applies ``_classify_regime``
    rather than relabelling: a clearly-burst z-score (>=2) must yield
    ``regime == "burst"`` from both paths.
    """

    assert _classify_regime(3.0) == "burst"
