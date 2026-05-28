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

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

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


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp, returning a tz-aware UTC ``datetime``.

    Naive strings are interpreted as UTC. Anything unparseable yields
    ``None`` so callers can fall through to a secondary field.
    """

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
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Prefer ``published_ts``; fall back to ``created_at``."""

    return _parse_ts(record.get("published_ts")) or _parse_ts(
        record.get("created_at")
    )


def _floor_bucket(ts: datetime, bucket_minutes: int) -> datetime:
    """Floor ``ts`` to the start of its ``bucket_minutes``-wide window.

    The anchor is the unix epoch — i.e. floors are aligned globally, so
    two reports built from overlapping data will share boundaries.
    """

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = ts - epoch
    bucket_seconds = bucket_minutes * 60
    floor_seconds = (int(delta.total_seconds()) // bucket_seconds) * bucket_seconds
    return epoch + timedelta(seconds=floor_seconds)


def _iso(ts: datetime) -> str:
    """Canonical ISO output (always trailing ``+00:00``, no microseconds)."""

    return ts.replace(microsecond=0).isoformat()


def _mass_spread(
    records: Iterable[Mapping[str, Any]], field: str
) -> dict[str, float]:
    """Distribute mass ``1/len(values)`` across each record's list-field.

    Records with empty or missing values contribute nothing. The returned
    mapping sums to the count of records that contributed (NOT 1.0) — the
    caller normalizes.
    """

    spread: dict[str, float] = {}
    for record in records:
        values = record.get(field) or []
        if not isinstance(values, (list, tuple)):
            continue
        cleaned = [str(v) for v in values if isinstance(v, str) and v]
        if not cleaned:
            continue
        share = 1.0 / len(cleaned)
        for key in cleaned:
            spread[key] = spread.get(key, 0.0) + share
    return spread


def _normalize(raw: Mapping[str, float]) -> dict[str, float]:
    """Convert a non-negative weight map into a probability distribution."""

    total = sum(raw.values())
    if total <= 0.0:
        return {}
    return {k: v / total for k, v in raw.items()}


def _top_n_sorted(dist: Mapping[str, float], n: int = _TOP_N) -> tuple[
    tuple[str, float], ...
]:
    """Top-N entries sorted DESC by probability, ties broken by key."""

    items = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(items[:n])


def _sentiment_distribution(
    records: Iterable[Mapping[str, Any]],
) -> tuple[tuple[str, float], ...]:
    """Count labelled records over the canonical 3-key sentiment vocab."""

    counts: dict[str, int] = {k: 0 for k in _SENTIMENT_KEYS}
    labelled = 0
    for record in records:
        label = record.get("sentiment_label")
        if label not in counts:
            continue
        counts[label] += 1
        labelled += 1
    if labelled == 0:
        return tuple((k, 0.0) for k in _SENTIMENT_KEYS)
    return tuple((k, counts[k] / labelled) for k in _SENTIMENT_KEYS)


def _mean_relevance(records: Iterable[Mapping[str, Any]]) -> float:
    """Arithmetic mean of ``finance_relevance_score`` across the bucket.

    Non-numeric or missing scores are skipped. Empty input returns 0.0.
    """

    scores: list[float] = []
    for record in records:
        raw = record.get("finance_relevance_score")
        if isinstance(raw, bool):  # bool subclasses int — guard first
            continue
        if isinstance(raw, (int, float)):
            scores.append(float(raw))
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _combine_for_kl(
    asset_dist: Mapping[str, float], reason_dist: Mapping[str, float]
) -> dict[str, float]:
    """Concatenate asset + reason distributions into one keyspace.

    Both sub-distributions are full (un-truncated) probability mass
    functions; combining them by namespacing keys with ``asset:`` /
    ``reason:`` prefixes keeps the totals at 2.0 pre-renormalization,
    which is fine because KL is invariant to a common scaling once both
    sides are renormalized.
    """

    combined: dict[str, float] = {}
    for k, v in asset_dist.items():
        combined[f"asset:{k}"] = v
    for k, v in reason_dist.items():
        combined[f"reason:{k}"] = v
    return combined


def _smooth_pair(
    p: Mapping[str, float], q: Mapping[str, float]
) -> tuple[dict[str, float], dict[str, float]]:
    """Epsilon-smooth two distributions over their union keyspace.

    Every key present in either side gets ``_EPSILON`` added on the side
    where it was missing; both are then renormalized so they sum to 1.0.
    """

    keys = set(p.keys()) | set(q.keys())
    if not keys:
        return {}, {}
    smoothed_p = {k: p.get(k, 0.0) + (_EPSILON if k not in p else 0.0) for k in keys}
    smoothed_q = {k: q.get(k, 0.0) + (_EPSILON if k not in q else 0.0) for k in keys}
    total_p = sum(smoothed_p.values())
    total_q = sum(smoothed_q.values())
    if total_p <= 0.0 or total_q <= 0.0:
        return {}, {}
    return (
        {k: v / total_p for k, v in smoothed_p.items()},
        {k: v / total_q for k, v in smoothed_q.items()},
    )


def _kl_divergence(p: Mapping[str, float], q: Mapping[str, float]) -> float:
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

    # Pair every record with its resolved timestamp; drop the unparseable.
    timed: list[tuple[datetime, Mapping[str, Any]]] = []
    for record in records or []:
        ts = _record_timestamp(record)
        if ts is None:
            continue
        timed.append((ts, record))

    if not timed:
        return RegimeReport(
            bucket_minutes=bucket_minutes,
            shift_threshold=shift_threshold,
            buckets=(),
            detected_shifts=(),
        )

    timed.sort(key=lambda pair: pair[0])
    width = timedelta(minutes=bucket_minutes)
    anchor = _floor_bucket(timed[0][0], bucket_minutes)

    # Group records by bucket-start ts (stable order via insertion).
    grouped: dict[datetime, list[Mapping[str, Any]]] = {}
    for ts, record in timed:
        bucket_start = anchor + timedelta(
            seconds=((ts - anchor).total_seconds() // (bucket_minutes * 60))
            * bucket_minutes
            * 60
        )
        grouped.setdefault(bucket_start, []).append(record)

    ordered_starts = sorted(grouped.keys())

    buckets: list[RegimeBucket] = []
    prev_asset_full: dict[str, float] = {}
    prev_reason_full: dict[str, float] = {}
    has_prev = False
    detected: list[str] = []

    for bucket_start in ordered_starts:
        bucket_records = grouped[bucket_start]
        bucket_end = bucket_start + width

        asset_full = _normalize(_mass_spread(bucket_records, "asset_classes"))
        reason_full = _normalize(
            _mass_spread(bucket_records, "impact_reason_codes")
        )
        sentiment_dist = _sentiment_distribution(bucket_records)
        mean_rel = _mean_relevance(bucket_records)

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
            adequate = (
                len(bucket_records) >= min_records_per_bucket
                and prev_count >= min_records_per_bucket
            )
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
