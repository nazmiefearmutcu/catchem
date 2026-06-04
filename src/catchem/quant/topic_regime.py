"""Topic-regime detection over catchem's ``FinancialImpactRecord`` stream.

Buckets records into fixed-width time windows, builds an asset-class /
reason-code / sentiment mixture per window, and flags transitions where
the topical composition diverges sharply from the previous window. The
intuition: a corpus dominated by ``equities + earnings`` that suddenly
pivots to ``rates + central_bank`` is a macro-tone regime change that
plain record-count metrics miss entirely.

Algorithm
---------
1. Bucket on ``published_ts`` (falling back to ``created_at``), anchored
   on the floor of the earliest record's timestamp at ``bucket_minutes``
   granularity.
2. Per bucket: record count, asset/reason mass-spread distributions
   (top 6 by probability, DESC), sentiment distribution, mean relevance.
3. Compare consecutive buckets via KL divergence on the concatenated
   asset+reason distribution. Epsilon-smooth (1e-3) any key missing from
   either side and renormalize before computing
   ``KL(p || q) = sum p_i * log(p_i / q_i)`` with natural log.
4. ``is_regime_shift`` fires when KL exceeds ``shift_threshold``.

This module is pure-function and read-only: it never mutates the input
records and produces deterministic output for a given input.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "RegimeBucket",
    "RegimeReport",
    "detect_regime_shifts",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EPSILON: float = 1e-3
_TOP_N: int = 6
_SENTIMENT_KEYS: tuple[str, ...] = ("positive", "neutral", "negative")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeBucket:
    """One fixed-width topical window.

    ``asset_distribution`` and ``reason_distribution`` are the top
    :data:`_TOP_N` entries sorted by probability DESC. The bucket also
    carries its KL divergence vs. the previous bucket (``None`` for the
    very first bucket) and a derived ``is_regime_shift`` flag.
    """

    bucket_start: str
    bucket_end: str
    record_count: int
    asset_distribution: tuple[tuple[str, float], ...]
    reason_distribution: tuple[tuple[str, float], ...]
    sentiment_distribution: tuple[tuple[str, float], ...]
    mean_relevance: float
    kl_divergence_from_prev: float | None
    is_regime_shift: bool


@dataclass(frozen=True)
class RegimeReport:
    """Top-level result wrapping one chronological run of buckets."""

    bucket_minutes: int
    shift_threshold: float
    buckets: tuple[RegimeBucket, ...]
    detected_shifts: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1024)
def _parse_ts_cached(raw_str: str) -> datetime | None:
    raw = raw_str.strip()
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
    """Parse an ISO timestamp, returning a tz-aware UTC ``datetime``.

    Naive strings are interpreted as UTC. Anything unparseable yields
    ``None`` so callers can fall through to a secondary field.
    """

    if not isinstance(value, str) or not value:
        return None
    return _parse_ts_cached(value)


def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Prefer ``published_ts``; fall back to ``created_at``."""

    return _parse_ts(record.get("published_ts")) or _parse_ts(record.get("created_at"))


@functools.lru_cache(maxsize=1024)
def _floor_bucket(ts: datetime, bucket_minutes: int) -> datetime:
    """Floor ``ts`` to the start of its ``bucket_minutes``-wide window.

    The anchor is the unix epoch — i.e. floors are aligned globally, so
    two reports built from overlapping data will share boundaries.
    """

    bucket_seconds = bucket_minutes * 60
    floor_seconds = (int(ts.timestamp()) // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floor_seconds, UTC)


@functools.lru_cache(maxsize=1024)
def _iso(ts: datetime) -> str:
    """Canonical ISO output (always trailing ``+00:00``, no microseconds)."""

    return ts.replace(microsecond=0).isoformat()


def _process_bucket_records(
    records: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[str, float],
    dict[str, float],
    tuple[tuple[str, float], ...],
    float,
]:
    asset_spread: dict[str, float] = {}
    reason_spread: dict[str, float] = {}
    pos_count = 0
    neu_count = 0
    neg_count = 0
    labelled = 0
    rel_scores: list[float] = []

    for record in records:
        # 1. Asset classes spread
        asset_vals = record.get("asset_classes")
        if isinstance(asset_vals, (list, tuple)):
            cleaned = [v for v in asset_vals if isinstance(v, str) and v]
            if cleaned:
                share = 1.0 / len(cleaned)
                for key in cleaned:
                    if key in asset_spread:
                        asset_spread[key] += share
                    else:
                        asset_spread[key] = share

        # 2. Reason codes spread
        reason_vals = record.get("impact_reason_codes")
        if isinstance(reason_vals, (list, tuple)):
            cleaned = [v for v in reason_vals if isinstance(v, str) and v]
            if cleaned:
                share = 1.0 / len(cleaned)
                for key in cleaned:
                    if key in reason_spread:
                        reason_spread[key] += share
                    else:
                        reason_spread[key] = share

        # 3. Sentiment distribution
        label = record.get("sentiment_label")
        if label == "positive":
            pos_count += 1
            labelled += 1
        elif label == "neutral":
            neu_count += 1
            labelled += 1
        elif label == "negative":
            neg_count += 1
            labelled += 1

        # 4. Relevance mean
        raw = record.get("finance_relevance_score")
        t = type(raw)
        if t is float:
            rel_scores.append(raw)
        elif t is int:
            rel_scores.append(float(raw))

    # Format sentiment
    if labelled == 0:
        sentiment_dist = (("positive", 0.0), ("neutral", 0.0), ("negative", 0.0))
    else:
        sentiment_dist = (
            ("positive", pos_count / labelled),
            ("neutral", neu_count / labelled),
            ("negative", neg_count / labelled),
        )

    # Format relevance
    mean_rel = sum(rel_scores) / len(rel_scores) if rel_scores else 0.0

    return asset_spread, reason_spread, sentiment_dist, mean_rel


def _normalize(raw: Mapping[str, float]) -> dict[str, float]:
    """Convert a non-negative weight map into a probability distribution."""

    total = sum(raw.values())
    if total <= 0.0:
        return {}
    return {k: v / total for k, v in raw.items()}


def _top_n_sorted(dist: Mapping[str, float], n: int = _TOP_N) -> tuple[tuple[str, float], ...]:
    """Top-N entries sorted DESC by probability, ties broken by key."""

    items = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(items[:n])


def _combine_for_kl(
    asset_dist: Mapping[str, float], reason_dist: Mapping[str, float]
) -> dict[tuple[str, str], float]:
    """Concatenate asset + reason distributions into one keyspace.

    Both sub-distributions are full (un-truncated) probability mass
    functions; combining them by namespacing keys with ``asset:`` /
    ``reason:`` prefixes keeps the totals at 2.0 pre-renormalization,
    which is fine because KL is invariant to a common scaling once both
    sides are renormalized.
    """

    combined: dict[tuple[str, str], float] = {}
    for k, v in asset_dist.items():
        combined[("asset", k)] = v
    for k, v in reason_dist.items():
        combined[("reason", k)] = v
    return combined


def _smooth_pair(p: Mapping[Any, float], q: Mapping[Any, float]) -> tuple[dict[Any, float], dict[Any, float]]:
    """Epsilon-smooth two distributions over their union keyspace.

    Every key present in either side gets ``_EPSILON`` added on the side
    where it was missing; both are then renormalized so they sum to 1.0.
    """

    keys = set(p.keys()) | set(q.keys())
    if not keys:
        return {}, {}
    smoothed_p = {}
    smoothed_q = {}
    total_p = 0.0
    total_q = 0.0
    for k in keys:
        val_p = p[k] if k in p else _EPSILON
        val_q = q[k] if k in q else _EPSILON
        smoothed_p[k] = val_p
        smoothed_q[k] = val_q
        total_p += val_p
        total_q += val_q
    if total_p <= 0.0 or total_q <= 0.0:
        return {}, {}
    return (
        {k: v / total_p for k, v in smoothed_p.items()},
        {k: v / total_q for k, v in smoothed_q.items()},
    )


def _kl_divergence(p: Mapping[Any, float], q: Mapping[Any, float]) -> float:
    """``KL(p || q)`` with natural log.

    Contributions where ``p_i <= 0`` are clamped to zero (the standard
    ``0 log 0 := 0`` convention). ``q`` is assumed pre-smoothed so no
    division-by-zero check is needed in the hot loop.
    """

    total = 0.0
    for key, p_i in p.items():
        if p_i <= 0.0:
            continue
        q_i = q.get(key, 0.0)
        if q_i <= 0.0:
            # _smooth_pair guarantees this never fires; defensive guard.
            continue
        total += p_i * math.log(p_i / q_i)
    return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_regime_shifts(
    records: list[dict],
    *,
    bucket_minutes: int = 60,
    shift_threshold: float = 0.40,
    min_records_per_bucket: int = 3,
) -> RegimeReport:
    """Detect topical-mix regime shifts in a stream of catchem records.

    Parameters
    ----------
    records:
        Iterable of ``FinancialImpactRecord``-shaped dicts. The function
        only reads ``published_ts``, ``created_at``, ``asset_classes``,
        ``impact_reason_codes``, ``finance_relevance_score`` and
        ``sentiment_label``.
    bucket_minutes:
        Width of each time window. Must be positive.
    shift_threshold:
        KL-divergence cutoff above which a bucket is marked as a regime
        shift. Tune downward to surface gentler pivots, upward to require
        sharper ones.

    Returns
    -------
    RegimeReport
        Chronologically ordered buckets plus the list of bucket-start
        timestamps where a shift fired.
    """

    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")

    # Group records by bucket-start ts (stable order via insertion).
    grouped: dict[datetime, list[Mapping[str, Any]]] = {}
    for record in records or []:
        ts = _record_timestamp(record)
        if ts is not None:
            bucket_start = _floor_bucket(ts, bucket_minutes)
            grouped.setdefault(bucket_start, []).append(record)

    if not grouped:
        return RegimeReport(
            bucket_minutes=bucket_minutes,
            shift_threshold=shift_threshold,
            buckets=(),
            detected_shifts=(),
        )

    ordered_starts = sorted(grouped.keys())
    width = timedelta(minutes=bucket_minutes)

    buckets: list[RegimeBucket] = []
    prev_asset_full: dict[str, float] = {}
    prev_reason_full: dict[str, float] = {}
    has_prev = False
    detected: list[str] = []

    for bucket_start in ordered_starts:
        bucket_records = grouped[bucket_start]
        bucket_end = bucket_start + width

        asset_spread, reason_spread, sentiment_dist, mean_rel = _process_bucket_records(bucket_records)
        asset_full = _normalize(asset_spread)
        reason_full = _normalize(reason_spread)

        if has_prev:
            p_combined = _combine_for_kl(asset_full, reason_full)
            q_combined = _combine_for_kl(prev_asset_full, prev_reason_full)
            p_smoothed, q_smoothed = _smooth_pair(p_combined, q_combined)
            kl: float | None = _kl_divergence(p_smoothed, q_smoothed)
            # Quality gate: KL on sparse buckets is dominated by the ε
            # smoother (see module docstring). Require both the current
            # bucket AND the previous bucket to carry enough records for
            # the KL signal to be meaningful. Below that, we still REPORT
            # the divergence but don't fire a shift — false positives in
            # the sparse tail were the #1 noise source.
            prev_count = buckets[-1].record_count if buckets else 0
            adequate = len(bucket_records) >= min_records_per_bucket and prev_count >= min_records_per_bucket
            is_shift = kl is not None and kl > shift_threshold and adequate
        else:
            kl = None
            is_shift = False

        bucket = RegimeBucket(
            bucket_start=_iso(bucket_start),
            bucket_end=_iso(bucket_end),
            record_count=len(bucket_records),
            asset_distribution=_top_n_sorted(asset_full),
            reason_distribution=_top_n_sorted(reason_full),
            sentiment_distribution=sentiment_dist,
            mean_relevance=mean_rel,
            kl_divergence_from_prev=kl,
            is_regime_shift=is_shift,
        )
        buckets.append(bucket)
        if is_shift:
            detected.append(bucket.bucket_start)

        prev_asset_full = asset_full
        prev_reason_full = reason_full
        has_prev = True

    return RegimeReport(
        bucket_minutes=bucket_minutes,
        shift_threshold=shift_threshold,
        buckets=tuple(buckets),
        detected_shifts=tuple(detected),
    )
