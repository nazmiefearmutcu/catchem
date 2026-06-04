"""Source reliability leaderboard.

Ranks news source domains across signal-quality axes so an analyst can see
which outlets consistently produce high-relevance, well-attributed content.

Pure functions only — no I/O, no storage mutation, no global state. Input
is a ``list[dict]`` of FinancialImpactRecord-shaped rows; output is an
immutable ``SourceLeaderboard``.

Axes:
  * relevant_rate            — gating quality (is the content finance?)
  * mean_relevance_score     — confidence on the relevant subset
  * signal_density           — fraction at the >=0.7 high-signal bar
  * |sentiment_skew|         — directional conviction (sign-agnostic)
  * asset_diversity          — Shannon-entropy breadth over asset_classes
  * reason_diversity         — Shannon-entropy breadth over impact reasons
  * symbol_uniqueness        — share of this domain's symbols nobody else mentioned

composite_score is the equal-weight mean, clamped to [0, 1].
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from math import log
from typing import Any

__all__ = [
    "SourceLeaderboard",
    "SourceScore",
    "compute_source_scores",
]


@dataclass(frozen=True)
class SourceScore:
    """Per-domain reliability snapshot.

    All ratio/diversity fields are in [0, 1] except ``sentiment_skew`` which is
    in [-1, +1]. ``composite_score`` is the equal-weight aggregate used for
    ranking.
    """

    domain: str
    record_count: int
    relevant_count: int
    relevant_rate: float
    mean_relevance_score: float
    signal_density: float
    sentiment_skew: float
    asset_diversity: float
    reason_diversity: float
    symbol_uniqueness: float
    composite_score: float


@dataclass(frozen=True)
class SourceLeaderboard:
    """Ranked container. ``sources`` is sorted by composite_score DESC."""

    window_days: int
    total_records: int
    total_domains: int
    sources: tuple[SourceScore, ...]


# ── helpers ────────────────────────────────────────────────────────────────

_UNKNOWN_DOMAIN = "(unknown)"


@lru_cache(maxsize=1024)
def _parse_ts_cached(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp string into a tz-aware UTC datetime.

    Returns ``None`` on any failure so the caller can fall back to another
    field. Naive timestamps are interpreted as UTC.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return _parse_ts_cached(s)


def _record_ts(rec: dict[str, Any]) -> datetime | None:
    """Pick the best available timestamp: published_ts, else created_at."""
    return _parse_ts(rec.get("published_ts")) or _parse_ts(rec.get("created_at"))


@lru_cache(maxsize=256)
def _normalize_domain_cached(s: str) -> str:
    stripped = s.strip().lower()
    return stripped or _UNKNOWN_DOMAIN


def _normalize_domain(value: Any) -> str:
    if not isinstance(value, str):
        return _UNKNOWN_DOMAIN
    return _normalize_domain_cached(value)


def _normalized_entropy(items: Iterable[str]) -> float:
    """Shannon entropy over a categorical sample, normalized to [0, 1].

    Missing / non-string items are skipped. A singleton (or empty) input
    returns 0 — there is no diversity to measure.
    """
    valid_items = [x for x in items if isinstance(x, str) and x]
    if len(valid_items) < 2:
        return 0.0
    counts = Counter(valid_items)
    if len(counts) < 2:
        return 0.0
    total = len(valid_items)
    entropy = -sum((c / total) * log(c / total) for c in counts.values())
    max_entropy = log(len(counts))
    if max_entropy == 0.0:
        return 0.0
    # Clamp because float rounding can push us a hair above 1.0.
    return max(0.0, min(1.0, entropy / max_entropy))


def _clamp01(value: float) -> float:
    if value != value:  # NaN guard
        return 0.0
    return max(0.0, min(1.0, value))


# ── public API ─────────────────────────────────────────────────────────────


def compute_source_scores(
    records: list[dict],
    *,
    window_days: int = 30,
    min_records: int = 3,
) -> SourceLeaderboard:
    """Build a ranked source-reliability leaderboard.

    Args:
        records: FinancialImpactRecord-shaped dicts. See module docstring.
        window_days: Only records with a timestamp within
            ``[now - window_days, now]`` are considered. Records with no
            parseable timestamp are dropped.
        min_records: Domains with fewer than this many in-window records
            are excluded from the leaderboard (but still counted in
            ``total_records``).

    Returns:
        ``SourceLeaderboard`` with sources sorted by composite_score DESC.
        Empty input returns an empty leaderboard.
    """
    if not records:
        return SourceLeaderboard(
            window_days=window_days,
            total_records=0,
            total_domains=0,
            sources=(),
        )

    # ── window filter ──
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    future_cutoff = now + timedelta(days=1)

    in_window: list[dict] = []
    for rec in records:
        ts = _record_ts(rec)
        if ts is None:
            continue
        if ts < cutoff or ts > future_cutoff:
            continue
        in_window.append(rec)

    if not in_window:
        return SourceLeaderboard(
            window_days=window_days,
            total_records=0,
            total_domains=0,
            sources=(),
        )

    # ── group by domain ──
    by_domain = defaultdict(list)
    for rec in in_window:
        d = _normalize_domain(rec.get("domain"))
        by_domain[d].append(rec)

    # Build the universe of "other-domain symbols" per domain on the fly.
    domain_symbol_sets: dict[str, set[str]] = {}
    symbol_domain_counts: dict[str, int] = {}
    for domain, rows in by_domain.items():
        symbols: set[str] = set()
        for rec in rows:
            candidate = rec.get("candidate_symbols")
            if candidate:
                for sym in candidate:
                    if isinstance(sym, str) and sym:
                        symbols.add(sym)
        domain_symbol_sets[domain] = symbols
        for sym in symbols:
            symbol_domain_counts[sym] = symbol_domain_counts.get(sym, 0) + 1

    scores: list[SourceScore] = []
    for domain, rows in by_domain.items():
        if len(rows) < min_records:
            continue

        record_count = len(rows)
        relevant_count = 0
        sum_relevance = 0.0
        high_signal_count = 0
        pos_count = 0
        neg_count = 0
        neu_count = 0
        all_assets: list[str] = []
        all_reasons: list[str] = []

        for r in rows:
            rev_val = r.get("finance_relevance_score")
            rev_score = float(rev_val) if rev_val else 0.0
            if rev_score >= 0.7:
                high_signal_count += 1
            if r.get("is_finance_relevant"):
                relevant_count += 1
                sum_relevance += rev_score

            sent = r.get("sentiment_label")
            if sent == "positive":
                pos_count += 1
            elif sent == "negative":
                neg_count += 1
            elif sent == "neutral":
                neu_count += 1

            assets = r.get("asset_classes")
            if assets:
                all_assets.extend(assets)
            reasons = r.get("impact_reason_codes")
            if reasons:
                all_reasons.extend(reasons)

        relevant_rate = relevant_count / record_count
        mean_relevance = sum_relevance / relevant_count if relevant_count > 0 else 0.0
        signal_density = high_signal_count / record_count

        sent_total = pos_count + neg_count + neu_count
        sentiment_skew = (pos_count - neg_count) / sent_total if sent_total > 0 else 0.0

        asset_diversity = _normalized_entropy(all_assets)
        reason_diversity = _normalized_entropy(all_reasons)

        # symbol_uniqueness: this domain's symbols not seen elsewhere.
        my_symbols = domain_symbol_sets[domain]
        if my_symbols:
            unique_count = sum(1 for sym in my_symbols if symbol_domain_counts[sym] == 1)
            symbol_uniqueness = unique_count / len(my_symbols)
        else:
            symbol_uniqueness = 0.0

        components = (
            _clamp01(relevant_rate),
            _clamp01(mean_relevance),
            _clamp01(signal_density),
            _clamp01(abs(sentiment_skew)),
            _clamp01(asset_diversity),
            _clamp01(reason_diversity),
            _clamp01(symbol_uniqueness),
        )
        composite = sum(components) / len(components)

        scores.append(
            SourceScore(
                domain=domain,
                record_count=record_count,
                relevant_count=relevant_count,
                relevant_rate=relevant_rate,
                mean_relevance_score=mean_relevance,
                signal_density=signal_density,
                sentiment_skew=sentiment_skew,
                asset_diversity=asset_diversity,
                reason_diversity=reason_diversity,
                symbol_uniqueness=symbol_uniqueness,
                composite_score=_clamp01(composite),
            )
        )

    # Rank: composite DESC, then record_count DESC, then domain ASC for
    # deterministic ordering across runs.
    scores.sort(key=lambda s: (-s.composite_score, -s.record_count, s.domain))

    return SourceLeaderboard(
        window_days=window_days,
        total_records=len(in_window),
        total_domains=len(by_domain),
        sources=tuple(scores),
    )
