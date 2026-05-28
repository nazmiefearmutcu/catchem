"""Cross-asset spillover detection for catchem's news stream.

Quants care less about *which* asset class is loud right now and more
about *who* tends to be loud right *before* somebody else. If every
rates surge is followed one window later by an equities surge, the
edge ``rates -> equities`` carries a real nowcast: when you see fresh
rates volume, you can pre-position equities exposure before that wave
shows up in price.

This module turns a flat record stream into a directional spillover
matrix:

    1. Bucket records by ``published_ts`` (fallback ``created_at``)
       into ``bucket_minutes``-wide windows, floored to the unix epoch
       so two overlapping reports share boundaries.
    2. Per asset class, compute a per-bucket rolling z-score against
       the **preceding** 8 buckets — strictly past-only, no lookahead.
       The first 3 buckets in the run are skipped (their history is
       too short to be meaningful).
    3. A bucket is a *surge* for asset ``X`` when its z-score is at
       or above ``surge_z_threshold``.
    4. For each ordered ``(source, target)`` asset pair, walk every
       eligible bucket ``i``: a co-movement is a bucket where source
       surged at ``i`` AND target surged at ``i + lag_buckets``. The
       module also counts source-only surges (source surged, target
       did not at the lagged offset) and target-only surges (target
       surged at lag without source preceding).
    5. spillover_score = P(target surge | source surge) - base rate of
       target surges. > 0 means the pair carries real conditional lift.
    6. Self-loops (``source == target``) are reported separately as
       persistence — they measure "rates keeps surging" not "rates
       triggers anything else".

Design constraints honoured:
  * stdlib-only (``datetime``, ``statistics``);
  * never mutates inputs;
  * deterministic ordering (edges sorted by score DESC, then by
    source/target alphabetical for stability).

The "surge" definition is *intentionally* asymmetric: we never peek
beyond bucket ``i`` to decide whether ``i`` is a surge for the source.
The target's surge at ``i + lag_buckets`` is computed from its own
preceding 8 buckets — none of which include the source's behaviour at
``i``. So spillover_score is a pure cross-correlation of past-only
z-scores, not a peek-ahead artefact.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

__all__ = [
    "SpilloverEdge",
    "SpilloverReport",
    "compute_spillover",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of preceding buckets used for the rolling z-score baseline.
# Eight gives enough samples that ``stdev`` is well-defined and a single
# noisy bucket can't dominate, while staying short enough to react to
# real regime changes.
_ROLLING_WINDOW: int = 8

# First buckets in the run get no z-score — their history is too short
# to be informative. Three matches the topic_regime "ramp" intuition:
# bucket 0/1/2 are warmup, bucket 3 is the first that gets evaluated
# (it has 3 historical buckets, which is the minimum for a meaningful
# stdev). Buckets 4..10 use whatever past they have; bucket 11 onward
# uses the full 8-wide window.
_MIN_HISTORY: int = 3

# Cap on the number of sample pivot timestamps echoed per edge.
_MAX_SAMPLE_PIVOTS: int = 3


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpilloverEdge:
    """One directional spillover relationship.

    ``co_movements`` is the count of buckets where ``source_asset``
    surged AND ``target_asset`` surged exactly ``lag_minutes`` later
    (``lag_minutes = lag_buckets * bucket_minutes``).

    ``spillover_score`` lives in ``[-1.0, 1.0+]`` — positive means the
    source consistently leads the target above what you'd expect from
    the target's unconditional surge rate. Values can technically
    exceed 1.0 if the base rate is very low and conditional rate is
    near 1.0; we don't clip the upper bound so callers can detect very
    tight couplings.
    """

    source_asset: str
    target_asset: str
    lag_minutes: int
    co_movements: int
    source_only_surges: int
    target_only_surges: int
    spillover_score: float
    sample_pivots: tuple[str, ...]


@dataclass(frozen=True)
class SpilloverReport:
    """Top-level result of one spillover sweep."""

    bucket_minutes: int
    lag_buckets: int
    surge_z_threshold: float
    edges: tuple[SpilloverEdge, ...]
    self_loops: tuple[SpilloverEdge, ...]
    total_buckets: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp into a tz-aware UTC datetime.

    Accepts the ``Z`` shortcut and naive strings (assumed UTC).
    Returns ``None`` for missing/unparseable input so callers can fall
    through to a secondary field.
    """

    if not isinstance(value, str) or not value:
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

    Anchored on the unix epoch so two reports built from overlapping
    data share boundaries.
    """

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = ts - epoch
    bucket_seconds = bucket_minutes * 60
    floor_seconds = (int(delta.total_seconds()) // bucket_seconds) * bucket_seconds
    return epoch + timedelta(seconds=floor_seconds)


def _iso(ts: datetime) -> str:
    """Canonical ISO output (always trailing ``+00:00``, no microseconds)."""

    return ts.replace(microsecond=0).isoformat()


def _asset_classes(record: Mapping[str, Any]) -> list[str]:
    """Pull a clean list of asset_class strings; drop blanks / non-strings."""

    raw = record.get("asset_classes") or []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        # A record with the same asset listed twice still only counts
        # once for that record's bucket contribution — otherwise a
        # noisy upstream emitting ``["rates", "rates"]`` would
        # silently double the bucket count.
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _rolling_z_score(counts: list[int], window: int) -> list[float | None]:
    """Compute a strictly past-only rolling z-score over ``counts``.

    For index ``i``:
      * if ``i < _MIN_HISTORY`` ⇒ ``None`` (insufficient warmup);
      * otherwise use the last ``min(window, i)`` values preceding
        ``i`` as the baseline. ``i`` itself is NOT in the baseline —
        that would leak the current bucket's value into its own
        threshold.

    ``stdev`` is the sample standard deviation. A zero-stdev baseline
    (every history bucket identical) maps to ``0.0`` for the current
    bucket regardless of the actual value — we can't claim a "surge"
    without a meaningful denominator, and infinity z-scores would
    poison every downstream pair count.
    """

    z_scores: list[float | None] = []
    for i in range(len(counts)):
        if i < _MIN_HISTORY:
            z_scores.append(None)
            continue
        start = max(0, i - window)
        history = counts[start:i]
        if len(history) < _MIN_HISTORY:
            # Defensive — _MIN_HISTORY <= start guarantees this, but if
            # callers ever lower the constant we don't want to crash.
            z_scores.append(None)
            continue
        mean = statistics.fmean(history)
        # ``pstdev`` would understate variance with small samples;
        # ``stdev`` (sample, n-1 denominator) is the right baseline.
        try:
            stdev = statistics.stdev(history)
        except statistics.StatisticsError:
            stdev = 0.0
        if stdev == 0.0:
            # No variance in the baseline — flat history. We refuse to
            # claim a surge because the z-score would either be 0 (when
            # the current bucket equals the flat baseline) or infinite
            # (when it differs). Returning 0.0 means "no surge" and is
            # the conservative choice; the rare false-negative beats a
            # certain false-positive from divide-by-near-zero noise.
            z_scores.append(0.0)
            continue
        z_scores.append((counts[i] - mean) / stdev)
    return z_scores


def _build_bucket_grid(
    timed: list[tuple[datetime, list[str]]],
    bucket_minutes: int,
) -> tuple[list[datetime], dict[str, list[int]]]:
    """Group records into a dense bucket grid keyed by asset class.

    Returns ``(ordered_bucket_starts, asset -> per_bucket_counts)``.
    The grid is *dense*: every bucket between min and max gets an
    entry, even if it has zero records. That matters because the
    z-score is computed against the preceding 8 buckets, and skipping
    empty buckets would let a sparse asset class artificially trigger
    surges on the next non-empty window.

    A record with two asset classes contributes 1 to each.
    """

    if not timed:
        return [], {}

    bucket_seconds = bucket_minutes * 60
    width = timedelta(minutes=bucket_minutes)
    first_ts = timed[0][0]
    anchor = _floor_bucket(first_ts, bucket_minutes)

    # Find the floor of the last record so we know the rightmost bucket
    # to materialize.
    last_floor = _floor_bucket(timed[-1][0], bucket_minutes)
    total_buckets = (
        int((last_floor - anchor).total_seconds()) // bucket_seconds
    ) + 1

    bucket_starts: list[datetime] = [
        anchor + timedelta(seconds=i * bucket_seconds) for i in range(total_buckets)
    ]

    asset_counts: dict[str, list[int]] = {}
    for ts, assets in timed:
        idx = int((ts - anchor).total_seconds()) // bucket_seconds
        for asset in assets:
            row = asset_counts.get(asset)
            if row is None:
                row = [0] * total_buckets
                asset_counts[asset] = row
            row[idx] += 1

    return bucket_starts, asset_counts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_spillover(
    records: list[dict],
    *,
    bucket_minutes: int = 30,
    lag_buckets: int = 1,
    surge_z_threshold: float = 1.5,
) -> SpilloverReport:
    """Compute a directional cross-asset spillover matrix.

    Parameters
    ----------
    records:
        Iterable of ``FinancialImpactRecord``-shaped dicts. Only
        ``published_ts``, ``created_at`` and ``asset_classes`` are
        read.
    bucket_minutes:
        Width of each time window. Must be positive.
    lag_buckets:
        How many buckets ahead to look for the target surge after the
        source surge fires. ``1`` is "the next window". Must be
        positive — a zero lag would conflate co-occurrence with
        spillover.
    surge_z_threshold:
        Minimum z-score for a bucket to count as a surge. ``1.5`` is
        a moderate threshold (~7% of normal-distributed buckets).
    """

    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    if lag_buckets <= 0:
        raise ValueError("lag_buckets must be positive")

    # Pair every record with its resolved timestamp + asset classes.
    timed: list[tuple[datetime, list[str]]] = []
    for record in records or []:
        ts = _record_timestamp(record)
        if ts is None:
            continue
        assets = _asset_classes(record)
        if not assets:
            # A record with no asset class can't contribute to any
            # per-asset bucket; skip silently.
            continue
        timed.append((ts, assets))

    if not timed:
        return SpilloverReport(
            bucket_minutes=bucket_minutes,
            lag_buckets=lag_buckets,
            surge_z_threshold=surge_z_threshold,
            edges=(),
            self_loops=(),
            total_buckets=0,
        )

    timed.sort(key=lambda pair: pair[0])
    bucket_starts, asset_counts = _build_bucket_grid(timed, bucket_minutes)
    total_buckets = len(bucket_starts)

    # No assets emerged (all records dropped) — bail.
    if not asset_counts or total_buckets == 0:
        return SpilloverReport(
            bucket_minutes=bucket_minutes,
            lag_buckets=lag_buckets,
            surge_z_threshold=surge_z_threshold,
            edges=(),
            self_loops=(),
            total_buckets=total_buckets,
        )

    # Per-asset z-score series, strictly past-only.
    z_by_asset: dict[str, list[float | None]] = {
        asset: _rolling_z_score(counts, _ROLLING_WINDOW)
        for asset, counts in asset_counts.items()
    }

    # Per-asset surge boolean series — None ⇒ not eligible (warmup),
    # True ⇒ surge, False ⇒ no surge.
    surges: dict[str, list[bool | None]] = {}
    for asset, zs in z_by_asset.items():
        surges[asset] = [
            None if z is None else (z >= surge_z_threshold) for z in zs
        ]

    assets_sorted = sorted(asset_counts.keys())
    lag_minutes = lag_buckets * bucket_minutes

    cross_edges: list[SpilloverEdge] = []
    self_loops: list[SpilloverEdge] = []

    # The trigger index ``i`` (source bucket) is only valid when:
    #   * i has a defined source z-score (i.e. surge value is True/False, not None);
    #   * i + lag_buckets is in range AND has a defined target z-score.
    # Both conditions kill any "warmup leak" — the lagged target also
    # needs at least _MIN_HISTORY of its own past.
    for source in assets_sorted:
        source_surges = surges[source]
        for target in assets_sorted:
            target_surges = surges[target]

            co_movements = 0
            source_only = 0
            target_only = 0
            sample_pivots: list[str] = []

            # Walk every potential trigger bucket; the eligibility set
            # is identical for every (source, target) pair so the base
            # rate denominator is consistent within the report.
            eligible_buckets = 0
            for i in range(total_buckets - lag_buckets):
                src_surge = source_surges[i]
                tgt_surge_at_lag = target_surges[i + lag_buckets]
                if src_surge is None or tgt_surge_at_lag is None:
                    continue
                eligible_buckets += 1
                if src_surge and tgt_surge_at_lag:
                    co_movements += 1
                    if len(sample_pivots) < _MAX_SAMPLE_PIVOTS:
                        sample_pivots.append(_iso(bucket_starts[i]))
                elif src_surge and not tgt_surge_at_lag:
                    source_only += 1
                elif tgt_surge_at_lag and not src_surge:
                    target_only += 1
                # not src_surge and not tgt_surge_at_lag: pure quiet,
                # not interesting for either numerator.

            # Conditional P(target surge | source surge).
            source_total = co_movements + source_only
            if source_total > 0:
                p_target_given_source = co_movements / source_total
            else:
                p_target_given_source = 0.0

            # Base rate of target surges in eligible bucket-pairs.
            # We use the *target's* surge incidence over eligible
            # bucket-pairs so the conditional and the base rate share
            # the same denominator structure (both restrict to buckets
            # where source AND target are out of warmup).
            target_surge_total = co_movements + target_only
            if eligible_buckets > 0:
                base_rate_target = target_surge_total / eligible_buckets
            else:
                base_rate_target = 0.0

            score = p_target_given_source - base_rate_target
            if score < -1.0:
                # Mathematically impossible since both terms are in
                # [0, 1], but guard anyway against future refactors.
                score = -1.0

            edge = SpilloverEdge(
                source_asset=source,
                target_asset=target,
                lag_minutes=lag_minutes,
                co_movements=co_movements,
                source_only_surges=source_only,
                target_only_surges=target_only,
                spillover_score=score,
                sample_pivots=tuple(sample_pivots),
            )

            if source == target:
                self_loops.append(edge)
            else:
                cross_edges.append(edge)

    # Cross-asset edges: keep only positive, well-supported ones.
    filtered = [
        e for e in cross_edges if e.spillover_score > 0.0 and e.co_movements >= 2
    ]
    # Sort by score DESC. Tiebreakers: more co-movements first
    # (stronger evidence), then alphabetical for stability.
    filtered.sort(
        key=lambda e: (
            -e.spillover_score,
            -e.co_movements,
            e.source_asset,
            e.target_asset,
        )
    )

    # Self-loops are reported unfiltered (callers may want to see
    # zero-persistence assets too) but sorted for determinism.
    self_loops.sort(
        key=lambda e: (
            -e.spillover_score,
            -e.co_movements,
            e.source_asset,
        )
    )

    return SpilloverReport(
        bucket_minutes=bucket_minutes,
        lag_buckets=lag_buckets,
        surge_z_threshold=surge_z_threshold,
        edges=tuple(filtered),
        self_loops=tuple(self_loops),
        total_buckets=total_buckets,
    )
