"""
Sentiment intensity signal.

For each record:
  intensity = finance_relevance_score * |sentiment_score|

High intensity = high-relevance + strongly polarized (pos or neg)
Low intensity = either irrelevant OR neutral sentiment

Per-symbol/per-asset aggregation:
  mean_intensity, max_intensity, count_high_intensity (>0.5)

Stdlib-only; defensive about missing / non-numeric inputs. Records with
no ``sentiment_score`` are treated as zero so they always fall out of
the high-intensity bucket without exploding the aggregate. The same
``_SCOPE_LABELS`` mapping the dispersion signal uses is reused here so
the UI can render the same ``asset_class:equities`` / ``symbol:BTC``
prefix style.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "IntensityBucket",
    "_record_intensity",
    "compute_by_scope",
    "compute_overall",
]


# Pretty singular labels for the scope prefix in compute_by_scope; kept in
# sync with the equivalent dict on sentiment_dispersion so the UI can use
# a single split-by-colon rule across both signals.
_SCOPE_LABELS: dict[str, str] = {
    "asset_classes": "asset_class",
    "candidate_symbols": "symbol",
    "reasons": "reason",
    "actor_types": "actor",
}

# Threshold for what counts as a "high-intensity" record. Picked to match
# the >0.5 bar in the module docstring — anything above this means the
# record is both highly relevant AND strongly polarized.
_HIGH_INTENSITY_CUTOFF: float = 0.5


@dataclass(frozen=True)
class IntensityBucket:
    """Per-scope intensity summary.

    ``count_high_intensity`` mirrors the >0.5 cutoff from the docstring;
    UI uses it as a "how many records crossed the bar" badge. ``top_records``
    is capped to keep payloads small — five rows per bucket lets the UI
    show a meaningful drill-down without ballooning the JSON.
    """

    scope: str
    sample_size: int
    mean_intensity: float
    max_intensity: float
    count_high_intensity: int  # count where intensity > 0.5
    top_records: list[dict]  # up to 5


def _coerce_float(value: Any) -> float:
    """Coerce a stored value to ``float``; junk → 0.0.

    Sentiment scores and relevance scores arrive from sqlite where
    they were stored as floats, but defensive ``None`` / string / bool
    handling matters when records come from a partial-replay path or
    from a hand-crafted demo payload. Bools are excluded because
    ``isinstance(True, int)`` is True and we don't want sentiment
    flags to silently coerce to 1.0.
    """

    t = type(value)
    if t is float:
        return value if math.isfinite(value) else 0.0
    if t is int:
        return float(value)
    if value is None or t is bool:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return f


def _finite_or_none(value: Any) -> float | None:
    """Finite-guard a passthrough numeric field, preserving ``None``.

    Unlike ``_coerce_float`` (which maps junk → 0.0 for the intensity math), the
    drill-down rows must NOT invent a 0.0 where the stored value was genuinely
    absent — but a non-finite NaN/Inf MUST be scrubbed to ``None`` because it
    would otherwise be embedded verbatim in the /api/quant/intensity response and
    crash Starlette's JSONResponse renderer (allow_nan=False) with HTTP 500.
    Mirrors schemas._finite_sentiment.
    """
    t = type(value)
    if t is float:
        return value if math.isfinite(value) else None
    if t is int:
        return float(value)
    if value is None or t is bool:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _record_intensity(record: dict) -> float:
    """Compute a single record's intensity score.

    ``intensity = relevance * |sentiment_score|`` — the absolute value
    matters because we want both bullish AND bearish polarization to
    surface (a strong negative read is just as actionable as a strong
    positive one). Returns 0.0 for any record missing one half.
    """

    relevance = record.get("finance_relevance_score")
    sentiment = record.get("sentiment_score")
    if type(relevance) is float and type(sentiment) is float:
        if math.isfinite(relevance) and math.isfinite(sentiment):
            return relevance * abs(sentiment)
    return _coerce_float(relevance) * abs(_coerce_float(sentiment))


def _build_top_records(
    pairs: Iterable[tuple[float, dict]],
    cap: int = 5,
) -> list[dict]:
    """Return up to ``cap`` records, sorted by intensity DESC.

    Tied intensities preserve upstream order (sort is stable) so the
    UI sees a deterministic drill-down ordering even when many records
    score identically.
    """

    sorted_pairs = sorted(pairs, key=lambda p: p[0], reverse=True)[:cap]
    return [
        {
            "capture_id": r.get("capture_id"),
            "title": r.get("title"),
            "intensity": intensity,
            # Finite-guard the raw passthroughs (None-preserving): a record loaded
            # via POST /api/db/import whose real columns hold NaN/Inf would
            # otherwise leak straight into the JSON response and 500 the panel.
            "score": _finite_or_none(r.get("finance_relevance_score")),
            "sentiment_label": r.get("sentiment_label"),
            "sentiment_score": _finite_or_none(r.get("sentiment_score")),
        }
        for intensity, r in sorted_pairs
    ]


def compute_overall(records: list[dict]) -> IntensityBucket:
    """Aggregate intensity across the entire record list.

    Empty input returns a zero-valued bucket with ``scope='overall'``
    so the UI envelope shape is identical to a populated response —
    no defensive None checks needed on the consumer side.
    """

    if not records:
        return IntensityBucket(
            scope="overall",
            sample_size=0,
            mean_intensity=0.0,
            max_intensity=0.0,
            count_high_intensity=0,
            top_records=[],
        )

    pairs: list[tuple[float, dict]] = []
    total_intensity = 0.0
    max_intensity = 0.0
    count_high = 0
    sample_size = 0

    for r in records:
        if not isinstance(r, dict):
            continue
        intensity = _record_intensity(r)
        total_intensity += intensity
        if intensity > max_intensity:
            max_intensity = intensity
        if intensity > _HIGH_INTENSITY_CUTOFF:
            count_high += 1
        sample_size += 1
        pairs.append((intensity, r))

    if sample_size == 0:
        return IntensityBucket(
            scope="overall",
            sample_size=0,
            mean_intensity=0.0,
            max_intensity=0.0,
            count_high_intensity=0,
            top_records=[],
        )

    mean = total_intensity / sample_size
    return IntensityBucket(
        scope="overall",
        sample_size=sample_size,
        mean_intensity=mean,
        max_intensity=max_intensity,
        count_high_intensity=count_high,
        top_records=_build_top_records(pairs),
    )


def compute_by_scope(
    records: list[dict],
    scope_key: str = "asset_classes",
) -> list[IntensityBucket]:
    """Bucket records by a scope key and aggregate intensity per bucket.

    ``scope_key`` is read off each record; list values lift every member
    (multi-asset stories raise every relevant bucket). Falsy values are
    skipped so we never get a "None" or empty-string fake bucket.

    Returns buckets sorted by ``mean_intensity`` DESC then bucket name
    ASC for deterministic ordering on ties.
    """

    if not records:
        return []

    buckets: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    scope_label = _SCOPE_LABELS.get(scope_key, scope_key)

    for r in records:
        if not isinstance(r, dict):
            continue
        intensity = _record_intensity(r)
        scope_value = r.get(scope_key)
        if isinstance(scope_value, list):
            for s in scope_value:
                if s and isinstance(s, str):
                    buckets[s].append((intensity, r))
        elif scope_value and isinstance(scope_value, str):
            buckets[scope_value].append((intensity, r))

    results: list[IntensityBucket] = []
    for bucket_name, items in buckets.items():
        if not items:
            continue
        total_intensity = 0.0
        max_i = items[0][0]
        high = 0
        for intensity, _ in items:
            total_intensity += intensity
            if intensity > max_i:
                max_i = intensity
            if intensity > _HIGH_INTENSITY_CUTOFF:
                high += 1
        mean = total_intensity / len(items)
        results.append(
            IntensityBucket(
                scope=f"{scope_label}:{bucket_name}",
                sample_size=len(items),
                mean_intensity=mean,
                max_intensity=max_i,
                count_high_intensity=high,
                top_records=_build_top_records(items),
            )
        )

    # Sort by mean DESC then by scope name ASC for stable cross-runs.
    results.sort(key=lambda b: (-b.mean_intensity, b.scope))
    return results
