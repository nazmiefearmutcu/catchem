"""Per-ticker sentiment momentum & velocity over the catchem record stream.

A single record only tells you that one outlet thinks AAPL is bearish right
now. What this module surfaces is whether AAPL's *tone* has flipped over the
last day — e.g. three buckets of positive coverage followed by three buckets
of negative coverage. That kind of trajectory does not show up in a single-
record view and is what we want to feed the Awareness Quant Lens.

Algorithm
---------
1. Bucket records by ``published_ts`` (falling back to ``created_at``),
   ``bucket_minutes`` wide, floor-anchored at the earliest record's
   timestamp. Records without a parseable timestamp are dropped.
2. Each record contributes ONE mention to EVERY symbol in its
   ``candidate_symbols`` list (so a record about AAPL + MSFT lifts both
   tickers' mention counts by 1 in that bucket).
3. Per (ticker, bucket): tally positive/neutral/negative counts (only over
   rows where ``sentiment_label`` is set) and average ``sentiment_score``
   over rows that carried a score. ``net_sentiment`` is
   ``(pos - neg) / count_with_label`` — the count is the number of rows
   with a sentiment label, NOT the total mention count.
4. Per ticker, build the chronological list of buckets where the ticker
   has at least one mention — **zero-mention gap buckets are skipped, not
   padded**, because padding a sparsely-covered ticker with empty bars
   would dwarf the actual signal in the momentum math.
5. Derive trajectory metrics:
   * ``overall_net_sentiment``: (total_positive - total_negative) /
     total_with_label across the full window.
   * ``momentum``: mean(net of LATER half of ticker's buckets) -
     mean(net of EARLIER half). Clamped to [-2, +2].
   * ``velocity``: mean of consecutive (net[i+1] - net[i]). 0 when the
     ticker only owns a single bucket.
   * ``direction``: a string label derived from first-half / last-half
     means using fixed thresholds (see module constants).
   * ``flip_detected``: the last 25% of buckets have opposite-sign mean
     net_sentiment from the first 75%.
6. Sort tickers by ``abs(momentum)`` DESC; truncate to ``max_tickers``.

This module is a pure function with no I/O, no extra dependencies and is
deterministic for a given input.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

__all__ = [
    "SentimentBucket",
    "TickerMomentum",
    "SentimentMomentumReport",
    "compute_sentiment_momentum",
]


# ---------------------------------------------------------------------------
# Direction thresholds (also exported for tests via direct import)
# ---------------------------------------------------------------------------

# A ticker has "flipped" between halves when the early and late half-means
# straddle these polarity bands.
_FLIP_POS_BAND: float = 0.20
_FLIP_NEG_BAND: float = -0.20

# "Strengthening" requires a meaningful jump (in absolute terms) AND the
# late half lands clearly on the relevant side of zero.
_STRENGTHEN_DELTA: float = 0.30

# Final flip detection uses sign of mean net_sentiment in the last 25% vs
# the first 75%. We require both halves to be off zero by a tiny margin so
# floating-point noise does not register as a flip.
_FLIP_DETECT_EPS: float = 1e-9

# Clamp band for momentum (early/late half-means each in [-1, +1] so the
# difference is naturally in [-2, +2]; clamp defensively anyway).
_MOMENTUM_CLAMP: float = 2.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SentimentBucket:
    """One ticker-bucket aggregate row.

    All fields describe a single ticker inside a single time window.
    ``count`` is the number of records in this bucket that mentioned the
    ticker; ``positive`` + ``neutral`` + ``negative`` may be smaller than
    ``count`` when some of those records carried no sentiment label.

    ``net_sentiment`` is normalized by the count of *labelled* rows (NOT
    ``count``) so an empty-label bucket cleanly reports 0.0 rather than
    pretending to be more confident than it is.
    """

    bucket_start: str
    bucket_end: str
    count: int
    positive: int
    neutral: int
    negative: int
    net_sentiment: float
    mean_score: float
    mean_relevance: float


@dataclass(frozen=True)
class TickerMomentum:
    """Per-ticker trajectory over the report window."""

    symbol: str
    mention_count: int
    buckets: tuple[SentimentBucket, ...]
    overall_net_sentiment: float
    momentum: float
    velocity: float
    direction: str
    flip_detected: bool
    last_bucket_start: str


@dataclass(frozen=True)
class SentimentMomentumReport:
    """Top-level result wrapping the ticker rankings."""

    bucket_minutes: int
    min_mentions: int
    tickers: tuple[TickerMomentum, ...]


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


def _floor_bucket(ts: datetime, anchor: datetime, bucket_minutes: int) -> datetime:
    """Floor ``ts`` onto the ``bucket_minutes``-wide grid anchored at ``anchor``.

    Anchored on the earliest record (not the unix epoch) so callers with
    short windows produce contiguous, human-readable bucket starts.
    """

    bucket_seconds = bucket_minutes * 60
    delta_seconds = int((ts - anchor).total_seconds())
    if delta_seconds < 0:
        # Defensive: anchor must be ≤ ts by construction, but guard anyway.
        delta_seconds = 0
    floor_seconds = (delta_seconds // bucket_seconds) * bucket_seconds
    return anchor + timedelta(seconds=floor_seconds)


def _iso(ts: datetime) -> str:
    """Canonical ISO output (always trailing ``+00:00``, no microseconds)."""

    return ts.replace(microsecond=0).isoformat()


def _safe_float(value: Any) -> float | None:
    """Best-effort float coercion; treat NaN/inf/garbage as ``None``."""

    if value is None or isinstance(value, bool):
        # bool subclasses int — exclude so True doesn't become 1.0.
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _clean_symbols(record: Mapping[str, Any]) -> list[str]:
    """Extract a de-duplicated, upper-cased symbol list from a record.

    Preserves first-seen order so callers see deterministic per-bucket
    insertion order downstream.
    """

    raw = record.get("candidate_symbols") or []
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        sym = item.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _clamp(value: float, lo: float, hi: float) -> float:
    """Standard clamp; also defangs floating-point overshoot."""

    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---------------------------------------------------------------------------
# Per-bucket aggregation
# ---------------------------------------------------------------------------


class _BucketAgg:
    """Mutable accumulator for one (ticker, bucket) cell.

    Kept as a tiny class (not a dataclass) because we hammer this in the
    inner loop and avoid a frozen-dataclass copy per record.
    """

    __slots__ = (
        "count",
        "positive",
        "neutral",
        "negative",
        "score_sum",
        "score_n",
        "relevance_sum",
        "relevance_n",
    )

    def __init__(self) -> None:
        self.count: int = 0
        self.positive: int = 0
        self.neutral: int = 0
        self.negative: int = 0
        self.score_sum: float = 0.0
        self.score_n: int = 0
        self.relevance_sum: float = 0.0
        self.relevance_n: int = 0

    def add(self, record: Mapping[str, Any]) -> None:
        self.count += 1

        label = record.get("sentiment_label")
        if label == "positive":
            self.positive += 1
        elif label == "neutral":
            self.neutral += 1
        elif label == "negative":
            self.negative += 1
        # any other value (None, "mixed", etc.) is ignored on purpose.

        score = _safe_float(record.get("sentiment_score"))
        if score is not None:
            self.score_sum += score
            self.score_n += 1

        rel = _safe_float(record.get("finance_relevance_score"))
        if rel is not None:
            self.relevance_sum += rel
            self.relevance_n += 1

    @property
    def labelled(self) -> int:
        return self.positive + self.neutral + self.negative

    @property
    def net_sentiment(self) -> float:
        n = self.labelled
        if n == 0:
            return 0.0
        return (self.positive - self.negative) / n

    @property
    def mean_score(self) -> float:
        if self.score_n == 0:
            return 0.0
        return self.score_sum / self.score_n

    @property
    def mean_relevance(self) -> float:
        if self.relevance_n == 0:
            return 0.0
        return self.relevance_sum / self.relevance_n


# ---------------------------------------------------------------------------
# Per-ticker trajectory math
# ---------------------------------------------------------------------------


def _classify_direction(first_half_mean: float, last_half_mean: float) -> str:
    """Return one of the five direction labels.

    Order matters: flips take precedence over strengthening so a clean
    sign-flip is never misread as "strengthening_negative".
    """

    # Flips first — they're the loudest signal we surface.
    if first_half_mean > _FLIP_POS_BAND and last_half_mean < _FLIP_NEG_BAND:
        return "flipping_negative"
    if first_half_mean < _FLIP_NEG_BAND and last_half_mean > _FLIP_POS_BAND:
        return "flipping_positive"

    delta = last_half_mean - first_half_mean
    if delta > _STRENGTHEN_DELTA and last_half_mean > _FLIP_POS_BAND:
        return "strengthening_positive"
    if delta < -_STRENGTHEN_DELTA and last_half_mean < _FLIP_NEG_BAND:
        return "strengthening_negative"
    return "stable"


def _detect_flip(nets: list[float]) -> bool:
    """Whether the last 25% of buckets sign-flipped from the first 75%.

    Uses ``math.ceil(n * 0.25)`` for the tail size so even small windows
    (2 buckets ⇒ 1-bucket tail) get a sensible split. Returns False if
    either half is essentially zero — we don't want FP noise to fire.
    """

    n = len(nets)
    if n < 2:
        return False
    tail_size = max(1, math.ceil(n * 0.25))
    head_size = n - tail_size
    if head_size < 1:
        return False
    head_mean = sum(nets[:head_size]) / head_size
    tail_mean = sum(nets[head_size:]) / tail_size
    if abs(head_mean) <= _FLIP_DETECT_EPS or abs(tail_mean) <= _FLIP_DETECT_EPS:
        return False
    return (head_mean > 0.0) != (tail_mean > 0.0)


def _half_means(nets: list[float]) -> tuple[float, float]:
    """Split ``nets`` in half by index and return (first_mean, last_mean).

    Odd-length lists put the median bucket in the LATE half (consistent
    with "later half includes the most-recent reading"). Single-bucket
    lists return the same value twice so callers can compute zero
    momentum without a special case.
    """

    n = len(nets)
    if n == 1:
        return nets[0], nets[0]
    split = n // 2  # for n=4 → split=2 (2/2); for n=5 → split=2 (2/3 late)
    first = nets[:split]
    last = nets[split:]
    return sum(first) / len(first), sum(last) / len(last)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def compute_sentiment_momentum(
    records: list[dict],
    *,
    bucket_minutes: int = 240,
    min_mentions: int = 4,
    max_tickers: int = 25,
) -> SentimentMomentumReport:
    """Compute per-ticker sentiment momentum across ``records``.

    Parameters
    ----------
    records:
        Iterable of catchem record dicts. Reads ``published_ts``,
        ``created_at``, ``candidate_symbols``, ``sentiment_label``,
        ``sentiment_score`` and ``finance_relevance_score``. Any record
        without a parseable timestamp or without a non-empty symbol list
        is silently skipped.
    bucket_minutes:
        Width of each time window. Must be positive.
    min_mentions:
        Minimum total mention count for a ticker to make the report.
    max_tickers:
        Cap on returned rows. Ties on ``|momentum|`` break on the
        symbol string for determinism.

    Returns
    -------
    SentimentMomentumReport
        Report with ``tickers`` sorted by ``|momentum|`` DESC.
    """

    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    if min_mentions < 1:
        raise ValueError("min_mentions must be >= 1")
    if max_tickers < 0:
        raise ValueError("max_tickers must be >= 0")

    # Pair every usable record with its resolved timestamp + cleaned symbols.
    paired: list[tuple[datetime, list[str], Mapping[str, Any]]] = []
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        ts = _record_timestamp(record)
        if ts is None:
            continue
        symbols = _clean_symbols(record)
        if not symbols:
            continue
        paired.append((ts, symbols, record))

    if not paired or max_tickers == 0:
        return SentimentMomentumReport(
            bucket_minutes=bucket_minutes,
            min_mentions=min_mentions,
            tickers=(),
        )

    paired.sort(key=lambda triple: triple[0])
    anchor = paired[0][0]
    width = timedelta(minutes=bucket_minutes)

    # (symbol, bucket_start) -> _BucketAgg. Two nested mappings would be
    # marginally cleaner but the flat tuple-keyed defaultdict is faster
    # and keeps mention totals trivial to derive.
    cells: dict[tuple[str, datetime], _BucketAgg] = defaultdict(_BucketAgg)
    per_symbol_buckets: dict[str, set[datetime]] = defaultdict(set)
    per_symbol_mentions: dict[str, int] = defaultdict(int)

    for ts, symbols, record in paired:
        bucket_start = _floor_bucket(ts, anchor, bucket_minutes)
        for sym in symbols:
            cells[(sym, bucket_start)].add(record)
            per_symbol_buckets[sym].add(bucket_start)
            per_symbol_mentions[sym] += 1

    tickers: list[TickerMomentum] = []
    for symbol, mention_count in per_symbol_mentions.items():
        if mention_count < min_mentions:
            continue

        ordered_starts = sorted(per_symbol_buckets[symbol])
        buckets: list[SentimentBucket] = []
        nets: list[float] = []
        total_positive = 0
        total_neutral = 0
        total_negative = 0

        for bucket_start in ordered_starts:
            agg = cells[(symbol, bucket_start)]
            buckets.append(
                SentimentBucket(
                    bucket_start=_iso(bucket_start),
                    bucket_end=_iso(bucket_start + width),
                    count=agg.count,
                    positive=agg.positive,
                    neutral=agg.neutral,
                    negative=agg.negative,
                    net_sentiment=agg.net_sentiment,
                    mean_score=agg.mean_score,
                    mean_relevance=agg.mean_relevance,
                )
            )
            nets.append(agg.net_sentiment)
            total_positive += agg.positive
            total_neutral += agg.neutral
            total_negative += agg.negative

        total_labelled = total_positive + total_neutral + total_negative
        if total_labelled > 0:
            overall_net = (total_positive - total_negative) / total_labelled
        else:
            overall_net = 0.0

        if len(nets) == 1:
            momentum = 0.0
            velocity = 0.0
            direction = "stable"
        else:
            first_mean, last_mean = _half_means(nets)
            momentum = _clamp(
                last_mean - first_mean, -_MOMENTUM_CLAMP, _MOMENTUM_CLAMP
            )
            # Mean of consecutive deltas — telescopes to
            # (nets[-1] - nets[0]) / (len(nets) - 1), but we compute it
            # explicitly so the intent is obvious from the code.
            deltas = [nets[i + 1] - nets[i] for i in range(len(nets) - 1)]
            velocity = sum(deltas) / len(deltas)
            direction = _classify_direction(first_mean, last_mean)

        flip_detected = _detect_flip(nets)

        tickers.append(
            TickerMomentum(
                symbol=symbol,
                mention_count=mention_count,
                buckets=tuple(buckets),
                overall_net_sentiment=overall_net,
                momentum=momentum,
                velocity=velocity,
                direction=direction,
                flip_detected=flip_detected,
                last_bucket_start=_iso(ordered_starts[-1]),
            )
        )

    # Sort by |momentum| DESC; break ties on symbol so the output is stable.
    tickers.sort(key=lambda t: (-abs(t.momentum), t.symbol))
    tickers = tickers[:max_tickers]

    return SentimentMomentumReport(
        bucket_minutes=bucket_minutes,
        min_mentions=min_mentions,
        tickers=tuple(tickers),
    )
