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

import functools
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


@functools.lru_cache(maxsize=4096)
def _parse_ts_cached(raw: str) -> datetime | None:
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


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp, returning a tz-aware UTC ``datetime``."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    return _parse_ts_cached(raw)


def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Prefer ``published_ts``; fall back to ``created_at``."""

    return _parse_ts(record.get("published_ts")) or _parse_ts(record.get("created_at"))


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

    n = len(prior)
    if n < _MIN_PRIOR_BUCKETS:
        return None
    mean = statistics.fmean(prior)
    use_fast = (
        getattr(statistics.stdev, "__name__", None) == "stdev"
        and getattr(statistics.stdev, "__module__", None) == "statistics"
    )
    if use_fast:
        variance = sum((x - mean) * (x - mean) for x in prior) / (n - 1)
        std = max(0.0, variance) ** 0.5
    else:
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
        if label == "positive":
            pos += 1
        elif label == "negative":
            neg += 1
        elif label == "neutral":
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

    timed: list[tuple[datetime, float, Mapping[str, Any], list[str]]] = []
    for record in records or []:
        ts = _record_timestamp(record)
        if ts is None:
            continue
        symbols = _symbols_in(record)
        timed.append((ts, ts.timestamp(), record, symbols))

    if not timed:
        return AnomalyReport(
            bucket_minutes=bucket_minutes,
            window_buckets=window_buckets,
            z_threshold=z_threshold,
            volume_anomalies=(),
            sentiment_shocks=(),
            symbol_bursts=(),
        )

    timed.sort(key=lambda pair: pair[1])
    first_ts, _, _, _ = timed[0]
    anchor = first_ts.replace(microsecond=0)
    anchor_minute = (anchor.minute // bucket_minutes) * bucket_minutes
    anchor = anchor.replace(minute=anchor_minute, second=0)
    anchor_epoch = int(anchor.timestamp())

    bucket_seconds = bucket_minutes * 60
    grouped_epochs: dict[int, list[tuple[Mapping[str, Any], list[str]]]] = {}
    for _, ts_epoch, record, symbols in timed:
        delta = ts_epoch - anchor_epoch
        floor_seconds = int(delta // bucket_seconds) * bucket_seconds
        bucket_start_epoch = anchor_epoch + floor_seconds
        grouped_epochs.setdefault(bucket_start_epoch, []).append((record, symbols))

    ordered_start_epochs = sorted(grouped_epochs.keys())

    # ---------------- Volume + sentiment passes ----------------
    volume_anomalies: list[VolumeAnomaly] = []
    sentiment_shocks: list[SentimentShock] = []

    prior_volumes: list[float] = []
    prior_nets: list[float] = []

    for bucket_start_epoch in ordered_start_epochs:
        bucket_records = grouped_epochs[bucket_start_epoch]
        bucket_end_epoch = bucket_start_epoch + bucket_seconds

        # ---- volume ----
        observed_volume = len(bucket_records)
        stats = _rolling_stats(prior_volumes[-window_buckets:])
        if stats is not None:
            mean, std = stats
            z = _z(float(observed_volume), mean, std)
            if abs(z) >= z_threshold:
                volume_anomalies.append(
                    VolumeAnomaly(
                        bucket_start=_iso(datetime.fromtimestamp(bucket_start_epoch, UTC)),
                        bucket_end=_iso(datetime.fromtimestamp(bucket_end_epoch, UTC)),
                        observed=observed_volume,
                        rolling_mean=mean,
                        rolling_std=std,
                        z_score=z,
                        severity=_severity(z),
                    )
                )
        prior_volumes.append(float(observed_volume))

        # ---- sentiment ----
        net = _net_sentiment([r for r, _ in bucket_records])
        if net is not None:
            stats = _rolling_stats(prior_nets[-window_buckets:])
            if stats is not None:
                mean, std = stats
                z = _z(net, mean, std)
                if abs(z) >= z_threshold:
                    sentiment_shocks.append(
                        SentimentShock(
                            bucket_start=_iso(datetime.fromtimestamp(bucket_start_epoch, UTC)),
                            bucket_end=_iso(datetime.fromtimestamp(bucket_end_epoch, UTC)),
                            observed_net=net,
                            rolling_mean=mean,
                            rolling_std=std,
                            z_score=z,
                            direction=_direction(z, net),
                        )
                    )
            prior_nets.append(net)

    # ---------------- Symbol burst pass ----------------
    per_symbol_counts: dict[str, dict[int, int]] = {}
    per_symbol_capture_ids: dict[str, dict[int, list[str]]] = {}

    for bucket_start_epoch in ordered_start_epochs:
        bucket_records = grouped_epochs[bucket_start_epoch]
        for record, symbols in bucket_records:
            capture_id = str(record.get("capture_id") or "")
            for symbol in symbols:
                counts = per_symbol_counts.setdefault(symbol, {})
                counts[bucket_start_epoch] = counts.get(bucket_start_epoch, 0) + 1
                ids = per_symbol_capture_ids.setdefault(symbol, {}).setdefault(bucket_start_epoch, [])
                if capture_id and len(ids) < 3:
                    ids.append(capture_id)

    symbol_bursts: list[SymbolBurst] = []
    for symbol, counts in per_symbol_counts.items():
        prior_counts: list[float] = []
        for i, bucket_start_epoch in enumerate(ordered_start_epochs):
            observed = counts.get(bucket_start_epoch, 0)
            if observed >= 2:
                start_idx = max(0, i - window_buckets)
                window = prior_counts[start_idx:i]
                n_win = len(window)
                if n_win >= 3:
                    mean = sum(window) / n_win
                    std = math.sqrt(max(1.0, mean))
                    z = (observed - mean) / std
                    if z >= z_threshold:
                        samples = tuple(per_symbol_capture_ids.get(symbol, {}).get(bucket_start_epoch, []))
                        symbol_bursts.append(
                            SymbolBurst(
                                symbol=symbol,
                                bucket_start=_iso(datetime.fromtimestamp(bucket_start_epoch, UTC)),
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
