"""
Cross-symbol correlation signal.

For a window of recent records, bucket by time (default 1h buckets).
For each pair of symbols (each appearing in >=2 buckets), compute
Pearson r over the bucket mention count vectors.

High positive r → symbols co-move in news flow (e.g., AAPL+MSFT on tech earnings days)
High negative r → mention surge in one coincides with quiet in the other
Near-zero r → independent narratives

Returns top-N highest |r| pairs.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

__all__ = [
    "SymbolPair",
    "compute_pairs",
]


@dataclass(frozen=True)
class SymbolPair:
    """One ordered symbol pair with its Pearson r over per-bucket mentions."""

    symbol_a: str
    symbol_b: str
    pearson_r: float
    n_buckets: int
    a_total: int
    b_total: int


# Upper bound on the dense bucket grid so a stale outlier timestamp can't
# allocate one slot per empty bucket across an unbounded span. ~20k buckets
# = ~830 days at the 60-minute default — far beyond any realistic window.
_MAX_GRID_BUCKETS = 20_000


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Sample Pearson r over two equal-length vectors.

    Returns 0.0 when the result is undefined (n<2 or zero variance on
    either side) rather than raising — high-volume callers don't want
    a single flat symbol vector to poison the whole pair sweep.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    sum_x = 0.0
    sum_y = 0.0
    for i in range(n):
        sum_x += xs[i]
        sum_y += ys[i]
    mx = sum_x / n
    my = sum_y / n

    cov = 0.0
    vx = 0.0
    vy = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        cov += dx * dy
        vx += dx * dx
        vy += dy * dy

    denom = math.sqrt(vx * vy)
    if denom == 0:
        return 0.0
    r = cov / denom
    # Numerical guard — accumulated FP error can push r a hair beyond
    # [-1, 1] on near-perfect inputs. Clamp so the UI never sees 1.0000001.
    if r > 1.0:
        return 1.0
    if r < -1.0:
        return -1.0
    return r


@lru_cache(maxsize=1024)
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


def _parse_ts(value: object) -> datetime | None:
    """Parse the same ISO shapes spillover accepts (Z suffix, naive=UTC)."""
    if type(value) is not str:
        return None
    raw = value.strip()
    if not raw:
        return None
    return _parse_ts_cached(raw)


def compute_pairs(
    records: list[dict],
    bucket_minutes: int = 60,
    min_mentions: int = 3,
    top_n: int = 30,
) -> list[SymbolPair]:
    """Return top-N symbol pairs by |Pearson r| over per-bucket mention counts.

    Parameters
    ----------
    records:
        FinancialImpactRecord-shaped dicts. Only ``published_ts``/
        ``created_at`` and ``candidate_symbols`` are consulted.
    bucket_minutes:
        Width of each time bucket (epoch-anchored so re-runs over
        overlapping windows share boundaries).
    min_mentions:
        A symbol must total at least this many mentions across the
        window to be considered. Filters out 1-shot tickers that would
        produce noisy near-zero r.
    top_n:
        Cap on the returned list. Sorted by |r| descending so callers
        see the strongest couplings first (positive AND negative).
    """
    if bucket_minutes <= 0:
        return []

    bucket_secs = bucket_minutes * 60

    # Bucket records by time, count each symbol mention. A symbol
    # listed twice in one record only counts once for that bucket —
    # otherwise a noisy upstream emitting duplicates would double the
    # bucket weight for that symbol.
    buckets: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in records or []:
        ts = _parse_ts(r.get("published_ts"))
        if ts is None:
            ts = _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        bucket_key = int(ts.timestamp()) // bucket_secs
        raw_symbols = r.get("candidate_symbols") or []
        if not isinstance(raw_symbols, list):
            continue
        seen: set[str] = set()
        for s in raw_symbols:
            if type(s) is not str:
                continue
            sym = s.strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            buckets[bucket_key][sym] += 1

    if len(buckets) < 2:
        # Pearson r needs at least 2 observations per series.
        return []

    # Count total mentions per symbol; keep only those >= min_mentions.
    symbol_totals: dict[str, int] = defaultdict(int)
    for bucket_counts in buckets.values():
        for sym, n in bucket_counts.items():
            symbol_totals[sym] += n

    eligible = sorted(s for s, n in symbol_totals.items() if n >= min_mentions)
    if len(eligible) < 2:
        return []

    # Build per-symbol vectors across a DENSE bucket grid spanning the full
    # observed window — every bucket between min and max, zero-filled where a
    # symbol wasn't mentioned. Earlier this iterated only sorted(buckets.keys())
    # i.e. the NON-EMPTY buckets, which collapsed the time axis: two symbols
    # surging in completely separate, distant periods landed in adjacent vector
    # positions and produced a spurious (often ±1.0) Pearson r that sorted to
    # the top of the panel. A dense grid preserves the temporal anchor — the
    # same approach spillover._build_bucket_grid uses. (The comment here always
    # CLAIMED density; only now is the grid actually dense across time.)
    min_bk, max_bk = min(buckets), max(buckets)
    # Defensive cap: a single stale record among recent ones could span an
    # enormous window and allocate one slot per empty bucket. Anchor to the
    # most recent _MAX_GRID_BUCKETS so memory stays bounded; symbols whose
    # mentions all predate the window become zero vectors (Pearson r = 0.0),
    # which is the correct "no contemporaneous signal" answer.
    if max_bk - min_bk + 1 > _MAX_GRID_BUCKETS:
        min_bk = max_bk - _MAX_GRID_BUCKETS + 1

    n_buckets = max_bk - min_bk + 1
    vectors: dict[str, list[float]] = {sym: [0.0] * n_buckets for sym in eligible}
    for bk, counts in buckets.items():
        if min_bk <= bk <= max_bk:
            idx = bk - min_bk
            for sym, count in counts.items():
                vec = vectors.get(sym)
                if vec is not None:
                    vec[idx] = float(count)

    # Pearson r for every unordered pair. eligible cap is small (≈50
    # post-filter) so O(n²) is fine.
    pairs: list[SymbolPair] = []
    for i, a in enumerate(eligible):
        for b in eligible[i + 1 :]:
            r = _pearson(vectors[a], vectors[b])
            pairs.append(
                SymbolPair(
                    symbol_a=a,
                    symbol_b=b,
                    pearson_r=r,
                    n_buckets=n_buckets,
                    a_total=symbol_totals[a],
                    b_total=symbol_totals[b],
                )
            )

    # Sort by |r| descending. Tiebreakers: higher combined volume
    # first (stronger evidence), then alphabetical for determinism.
    pairs.sort(
        key=lambda p: (
            -abs(p.pearson_r),
            -(p.a_total + p.b_total),
            p.symbol_a,
            p.symbol_b,
        )
    )
    return pairs[:top_n]
