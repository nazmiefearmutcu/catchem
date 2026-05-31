"""
News velocity signal — measures how quickly news is arriving and whether
the rate is accelerating or decelerating.

Computes:
- ``current_rate_per_min`` — records/min in the most recent bucket
- ``ema_fast`` (alpha=0.3) and ``ema_slow`` (alpha=0.05) — EMAs over the bucket
  sequence; ``ema_fast`` reacts quickly to fresh arrivals while
  ``ema_slow`` carries the slower baseline used for divergence checks.
- ``baseline_rate`` — median bucket rate over the window (robust to
  bursts; one big spike doesn't poison it).
- ``baseline_std`` — sample stdev across the bucket sequence.
- ``acceleration_z`` — ``(current_rate - baseline_rate) / baseline_std``
  expressed as a z-score so the regime classifier is amplitude-free.
- ``regime`` — ``"calm" | "active" | "burst" | "quiet"`` keyed on
  ``acceleration_z``.

High velocity + positive ``acceleration_z`` = news flow accelerating.
Low velocity + negative ``acceleration_z`` = market quiet.

The module is pure-function and stdlib-only (``math`` + ``statistics``).
Bad timestamps are dropped silently; an empty input yields an
all-zero report rather than raising.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["VelocityReport", "compute_velocity"]


@dataclass(frozen=True)
class VelocityReport:
    """Top-level news velocity result."""

    current_rate_per_min: float
    ema_fast: float
    ema_slow: float
    baseline_rate: float
    baseline_std: float
    acceleration_z: float
    regime: str
    bucket_minutes: int
    window_minutes: int
    samples: int


_ALPHA_FAST: float = 0.3
_ALPHA_SLOW: float = 0.05
_REGIME_BURST: float = 2.0
_REGIME_ACTIVE: float = 1.0
_REGIME_QUIET: float = -1.0


def _classify_regime(z: float) -> str:
    """Map an acceleration z-score onto the four regime labels."""

    if math.isnan(z) or math.isinf(z):
        return "calm"
    if z >= _REGIME_BURST:
        return "burst"
    if z >= _REGIME_ACTIVE:
        return "active"
    if z <= _REGIME_QUIET:
        return "quiet"
    return "calm"


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp, returning a tz-aware UTC ``datetime``.

    Returns ``None`` for non-string, empty, or unparseable input so the
    caller can drop bad rows without exception handling.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed


def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Prefer ``published_ts``; fall back to ``created_at``."""

    return _parse_ts(record.get("published_ts")) or _parse_ts(
        record.get("created_at")
    )


def _empty(bucket_minutes: int, window_minutes: int) -> VelocityReport:
    return VelocityReport(
        current_rate_per_min=0.0,
        ema_fast=0.0,
        ema_slow=0.0,
        baseline_rate=0.0,
        baseline_std=0.0,
        acceleration_z=0.0,
        regime="calm",
        bucket_minutes=bucket_minutes,
        window_minutes=window_minutes,
        samples=0,
    )


def compute_velocity(
    records: list[dict] | list[Mapping[str, Any]],
    bucket_minutes: int = 5,
    window_minutes: int = 60,
) -> VelocityReport:
    """Compute news velocity over the recent window.

    Parameters
    ----------
    records:
        Iterable of ``FinancialImpactRecord``-shaped dicts. Only
        ``published_ts`` / ``created_at`` are read.
    bucket_minutes:
        Width of each bucket. Must be positive.
    window_minutes:
        Trailing window from the latest timestamp. Must be positive.

    Returns
    -------
    VelocityReport
        All-zero ``regime="calm"`` report if no records carry a parseable
        timestamp or fall inside the window.
    """

    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")

    if not records:
        return _empty(bucket_minutes, window_minutes)

    timestamps: list[datetime] = []
    for record in records:
        ts = _record_timestamp(record)
        if ts is not None:
            timestamps.append(ts)

    if not timestamps:
        return _empty(bucket_minutes, window_minutes)

    latest = max(timestamps)
    window_start = latest - timedelta(minutes=window_minutes)
    in_window = [ts for ts in timestamps if ts >= window_start]

    if not in_window:
        return _empty(bucket_minutes, window_minutes)

    # Bucket using epoch-second arithmetic — no anchor drift across calls
    # because every bucket key is `floor(epoch / bucket_seconds)`.
    bucket_secs = bucket_minutes * 60
    bucket_counts: Counter[int] = Counter()
    for ts in in_window:
        bk = int(ts.timestamp()) // bucket_secs
        bucket_counts[bk] += 1

    earliest_bucket = int(window_start.timestamp()) // bucket_secs
    latest_bucket = int(latest.timestamp()) // bucket_secs

    # Fill zeros across the full bucket range so EMAs and the baseline
    # see a true rate timeseries (a 0-arrival bucket IS information).
    sequence: list[float] = []
    for b in range(earliest_bucket, latest_bucket + 1):
        count = bucket_counts.get(b, 0)
        sequence.append(count / bucket_minutes)

    if not sequence:
        return _empty(bucket_minutes, window_minutes)

    current_rate = sequence[-1]

    # Recursive EMA — seed both with sequence[0] so a steady stream
    # converges immediately rather than drifting up from 0.
    ema_fast = sequence[0]
    ema_slow = sequence[0]
    for x in sequence[1:]:
        ema_fast = _ALPHA_FAST * x + (1.0 - _ALPHA_FAST) * ema_fast
        ema_slow = _ALPHA_SLOW * x + (1.0 - _ALPHA_SLOW) * ema_slow

    baseline_rate = statistics.median(sequence)
    if len(sequence) >= 2:
        try:
            baseline_std = statistics.stdev(sequence)
        except statistics.StatisticsError:
            baseline_std = 0.0
    else:
        baseline_std = 0.0
    if not math.isfinite(baseline_std) or baseline_std < 0.0:
        baseline_std = 0.0

    if baseline_std > 0.0:
        accel = (current_rate - baseline_rate) / baseline_std
    else:
        accel = 0.0

    return VelocityReport(
        current_rate_per_min=current_rate,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        baseline_rate=baseline_rate,
        baseline_std=baseline_std,
        acceleration_z=accel,
        regime=_classify_regime(accel),
        bucket_minutes=bucket_minutes,
        window_minutes=window_minutes,
        samples=len(in_window),
    )
