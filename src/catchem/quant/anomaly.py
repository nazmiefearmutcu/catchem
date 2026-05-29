"""Anomaly detection over rolling windows of catchem's news stream.

Surfaces three independently-axed anomalies in a single pass:

1. **Volume spikes** — record counts in a bucket that overshoot the recent
   rolling mean by a z-score threshold.
2. **Sentiment shocks** — bucket-level net sentiment (``(pos - neg) / N``)
   that deviates from its own rolling baseline.
3. **Symbol bursts** — per-symbol mention counts in a bucket that exceed
   that symbol's own rolling typical.

Bucketing
---------
Buckets are floor-anchored at the earliest record's timestamp and tile
forward in fixed ``bucket_minutes`` windows. ``published_ts`` is preferred;
``created_at`` is the fallback. Unparseable records are dropped.

Rolling statistics
------------------
For volume and sentiment we use sample mean / sample std over the
**preceding** ``window_buckets`` (current bucket excluded). At least 3
prior observations are required; otherwise the bucket is skipped — sample
std is too noisy below that. ``std <= 0`` is treated as ``z = 0`` so flat
windows can't produce divide-by-zero infinities.

Symbol burst z-score deliberately uses a Poisson-like std,
``sqrt(max(1, rolling_mean))``, instead of the sample std. Mention counts
for the long tail of tickers are sparse and integer-valued, so the sample
std collapses to ~0 across runs of zeros and would produce spurious
infinities. The Poisson surrogate is the textbook variance for a count
process and stays well-behaved on rare-event symbols.

The module is pure-function and stdlib-only: ``math`` + ``statistics``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "AnomalyReport",
    "SentimentShock",
    "SymbolBurst",
    "VolumeAnomaly",
    "detect_anomalies",
]


_SENTIMENT_LABELS: frozenset[str] = frozenset({"positive", "neutral", "negative"})
_MIN_PRIOR_BUCKETS: int = 3
_SAMPLE_CAPTURE_IDS_PER_BURST: int = 3
_NET_LEVEL_THRESHOLD: float = 0.20


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeAnomaly:
    """A bucket whose record count broke from its rolling baseline."""

    bucket_start: str
    bucket_end: str
    observed: int
    rolling_mean: float
    rolling_std: float
    z_score: float
    severity: str


@dataclass(frozen=True)
class SentimentShock:
    """A bucket whose net sentiment broke from its rolling baseline."""

    bucket_start: str
    bucket_end: str
    observed_net: float
    rolling_mean: float
    rolling_std: float
    z_score: float
    direction: str


@dataclass(frozen=True)
class SymbolBurst:
    """A symbol whose mentions in a bucket exceeded its rolling typical."""

    symbol: str
    bucket_start: str
    observed: int
    rolling_mean: float
    z_score: float
    sample_capture_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnomalyReport:
    """Top-level result wrapping the three anomaly axes."""

    bucket_minutes: int
    window_buckets: int
    z_threshold: float
    volume_anomalies: tuple[VolumeAnomaly, ...]
    sentiment_shocks: tuple[SentimentShock, ...]
    symbol_bursts: tuple[SymbolBurst, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp, returning a tz-aware UTC ``datetime``."""

    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
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


def _iso(ts: datetime) -> str:
    """Canonical ISO output (always trailing ``+00:00``, no microseconds)."""

    return ts.replace(microsecond=0).isoformat()


def _bucket_start_for(ts: datetime, anchor: datetime, bucket_minutes: int) -> datetime:
    """Project ``ts`` onto the bucket-grid anchored at ``anchor``."""

    bucket_seconds = bucket_minutes * 60
    delta = (ts - anchor).total_seconds()
    floor_seconds = (int(delta) // bucket_seconds) * bucket_seconds
    return anchor + timedelta(seconds=floor_seconds)


def _rolling_stats(prior: list[float]) -> tuple[float, float] | None:
    """Sample mean + sample std over ``prior``.

    Returns ``None`` when ``len(prior) < _MIN_PRIOR_BUCKETS``. ``std`` is
    clamped to 0.0 if the variance is non-positive — callers convert that
    to ``z = 0`` so flat history can't produce infinities.
    """

    if len(prior) < _MIN_PRIOR_BUCKETS:
        return None
    mean = statistics.fmean(prior)
    try:
        std = statistics.stdev(prior)
    except statistics.StatisticsError:
        std = 0.0
    if std <= 0.0 or math.isnan(std):
        std = 0.0
    return mean, std


def _z(observed: float, mean: float, std: float) -> float:
    """Safe z-score: ``std <= 0`` collapses to 0."""

    if std <= 0.0:
        return 0.0
    return (observed - mean) / std


def _severity(z: float) -> str:
    """Three-tier severity ladder keyed on ``|z|``."""

    abs_z = abs(z)
    if abs_z >= 4.0:
        return "high"
    if abs_z >= 3.0:
        return "medium"
    return "low"


def _direction(z: float, observed_net: float) -> str:
    """Classify a sentiment shock by both direction and absolute level."""

    if z > 0 and observed_net > _NET_LEVEL_THRESHOLD:
        return "bullish_shock"
    if z < 0 and observed_net < -_NET_LEVEL_THRESHOLD:
        return "bearish_shock"
    return "neutral"


def _net_sentiment(records: list[Mapping[str, Any]]) -> float | None:
    """``(pos - neg) / N`` over the sentiment-labelled subset.

    Returns ``None`` when no record carries a known sentiment label so the
    caller can skip the bucket cleanly (vs. logging a spurious 0.0).
    """

    pos = neu = neg = 0
    for record in records:
        label = record.get("sentiment_label")
        if label not in _SENTIMENT_LABELS:
            continue
        if label == "positive":
            pos += 1
        elif label == "negative":
            neg += 1
        else:
            neu += 1
    total = pos + neu + neg
    if total == 0:
        return None
    return (pos - neg) / total


def _symbols_in(record: Mapping[str, Any]) -> list[str]:
    """Extract a clean, de-duped list of candidate symbols from a record."""

    raw = record.get("candidate_symbols") or []
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in raw:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_anomalies(
    records: list[dict],
    *,
    bucket_minutes: int = 30,
    window_buckets: int = 12,
    z_threshold: float = 2.0,
    top_n_symbols: int = 25,
) -> AnomalyReport:
    """Run the three-axis anomaly scan over a stream of catchem records.

    Parameters
    ----------
    records:
        Iterable of ``FinancialImpactRecord``-shaped dicts. Only
        ``published_ts``/``created_at``, ``sentiment_label``,
        ``candidate_symbols`` and ``capture_id`` are read.
    bucket_minutes:
        Width of each time window. Must be positive.
    window_buckets:
        Rolling window size in buckets. Sample stats use the **preceding**
        ``window_buckets`` (current bucket excluded). Must be positive.
    z_threshold:
        Absolute z-score above which an observation is flagged.
    top_n_symbols:
        Cap on the symbol-burst output. Results are sorted by ``z_score``
        DESC before truncation.

    Returns
    -------
    AnomalyReport
        Volume + sentiment anomalies in chronological order; symbol bursts
        sorted by z-score DESC and capped at ``top_n_symbols``.
    """

    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    if window_buckets <= 0:
        raise ValueError("window_buckets must be positive")

    timed: list[tuple[datetime, Mapping[str, Any]]] = []
    for record in records or []:
        ts = _record_timestamp(record)
        if ts is None:
            continue
        timed.append((ts, record))

    if not timed:
        return AnomalyReport(
            bucket_minutes=bucket_minutes,
            window_buckets=window_buckets,
            z_threshold=z_threshold,
            volume_anomalies=(),
            sentiment_shocks=(),
            symbol_bursts=(),
        )

    timed.sort(key=lambda pair: pair[0])
    anchor = timed[0][0].replace(microsecond=0)
    # Floor the anchor to its own bucket-minutes boundary so the first
    # bucket's start = the floor of the earliest timestamp.
    anchor_minute = (anchor.minute // bucket_minutes) * bucket_minutes
    anchor = anchor.replace(minute=anchor_minute, second=0)

    width = timedelta(minutes=bucket_minutes)
    grouped: dict[datetime, list[Mapping[str, Any]]] = {}
    for ts, record in timed:
        bucket_start = _bucket_start_for(ts, anchor, bucket_minutes)
        grouped.setdefault(bucket_start, []).append(record)

    ordered_starts = sorted(grouped.keys())

    # ---------------- Volume + sentiment passes ----------------
    volume_anomalies: list[VolumeAnomaly] = []
    sentiment_shocks: list[SentimentShock] = []

    prior_volumes: list[float] = []
    prior_nets: list[float] = []

    for bucket_start in ordered_starts:
        bucket_records = grouped[bucket_start]
        bucket_end = bucket_start + width

        # ---- volume ----
        observed_volume = len(bucket_records)
        stats = _rolling_stats(prior_volumes[-window_buckets:])
        if stats is not None:
            mean, std = stats
            z = _z(float(observed_volume), mean, std)
            if abs(z) >= z_threshold:
                volume_anomalies.append(
                    VolumeAnomaly(
                        bucket_start=_iso(bucket_start),
                        bucket_end=_iso(bucket_end),
                        observed=observed_volume,
                        rolling_mean=mean,
                        rolling_std=std,
                        z_score=z,
                        severity=_severity(z),
                    )
                )
        prior_volumes.append(float(observed_volume))

        # ---- sentiment ----
        net = _net_sentiment(bucket_records)
        if net is not None:
            stats = _rolling_stats(prior_nets[-window_buckets:])
            if stats is not None:
                mean, std = stats
                z = _z(net, mean, std)
                if abs(z) >= z_threshold:
                    sentiment_shocks.append(
                        SentimentShock(
                            bucket_start=_iso(bucket_start),
                            bucket_end=_iso(bucket_end),
                            observed_net=net,
                            rolling_mean=mean,
                            rolling_std=std,
                            z_score=z,
                            direction=_direction(z, net),
                        )
                    )
            prior_nets.append(net)
        # buckets with no sentiment-labelled rows don't update the baseline

    # ---------------- Symbol burst pass ----------------
    # Build per-symbol, per-bucket count + capture-id maps. Iterate
    # symbol-by-symbol so each symbol gets its own rolling history that
    # ignores other symbols' buckets entirely.
    per_symbol_counts: dict[str, dict[datetime, int]] = {}
    per_symbol_capture_ids: dict[str, dict[datetime, list[str]]] = {}

    for bucket_start in ordered_starts:
        bucket_records = grouped[bucket_start]
        for record in bucket_records:
            capture_id = str(record.get("capture_id") or "")
            for symbol in _symbols_in(record):
                counts = per_symbol_counts.setdefault(symbol, {})
                counts[bucket_start] = counts.get(bucket_start, 0) + 1
                ids = per_symbol_capture_ids.setdefault(symbol, {}).setdefault(
                    bucket_start, []
                )
                if capture_id and len(ids) < _SAMPLE_CAPTURE_IDS_PER_BURST:
                    ids.append(capture_id)

    symbol_bursts: list[SymbolBurst] = []
    for symbol, counts in per_symbol_counts.items():
        prior_counts: list[float] = []
        for bucket_start in ordered_starts:
            observed = counts.get(bucket_start, 0)
            window = prior_counts[-window_buckets:]
            if observed >= 2 and len(window) >= _MIN_PRIOR_BUCKETS:
                mean = statistics.fmean(window)
                std = math.sqrt(max(1.0, mean))  # Poisson-like surrogate
                z = (observed - mean) / std
                if z >= z_threshold:
                    samples = tuple(
                        per_symbol_capture_ids.get(symbol, {}).get(bucket_start, [])
                    )
                    symbol_bursts.append(
                        SymbolBurst(
                            symbol=symbol,
                            bucket_start=_iso(bucket_start),
                            observed=observed,
                            rolling_mean=mean,
                            z_score=z,
                            sample_capture_ids=samples,
                        )
                    )
            prior_counts.append(float(observed))

    symbol_bursts.sort(key=lambda b: (-b.z_score, b.symbol, b.bucket_start))
    symbol_bursts = symbol_bursts[:top_n_symbols]

    return AnomalyReport(
        bucket_minutes=bucket_minutes,
        window_buckets=window_buckets,
        z_threshold=z_threshold,
        volume_anomalies=tuple(volume_anomalies),
        sentiment_shocks=tuple(sentiment_shocks),
        symbol_bursts=tuple(symbol_bursts),
    )
