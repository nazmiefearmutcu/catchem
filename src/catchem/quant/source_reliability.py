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

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO timestamp string into a tz-aware UTC datetime.

    Returns ``None`` on any failure so the caller can fall back to another
    field. Naive timestamps are interpreted as UTC.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    # `fromisoformat` (3.11+) accepts trailing "Z"; older parsers do not.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _record_ts(rec: dict[str, Any]) -> datetime | None:
    """Pick the best available timestamp: published_ts, else created_at."""
    return _parse_ts(rec.get("published_ts")) or _parse_ts(rec.get("created_at"))


def _normalize_domain(value: Any) -> str:
    if not isinstance(value, str):
        return _UNKNOWN_DOMAIN
    stripped = value.strip().lower()
    return stripped or _UNKNOWN_DOMAIN


def _normalized_entropy(items: Iterable[str]) -> float:
    """Shannon entropy over a categorical sample, normalized to [0, 1].

    Missing / non-string items are skipped. A singleton (or empty) input
    returns 0 — there is no diversity to measure.
    """
    counts = Counter(x for x in items if isinstance(x, str) and x)
    if len(counts) < 2:
        return 0.0
    total = sum(counts.values())
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

    in_window: list[dict] = []
    for rec in records:
        ts = _record_ts(rec)
        if ts is None:
            continue
        if ts < cutoff or ts > now + timedelta(days=1):
            # Reject far-future timestamps too; clock skew, but a record
            # dated next year should not pollute "last N days" analytics.
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
    by_domain: dict[str, list[dict]] = {}
    for rec in in_window:
        d = _normalize_domain(rec.get("domain"))
        by_domain.setdefault(d, []).append(rec)

    # Build the universe of "other-domain symbols" per domain on the fly.
    domain_symbol_sets: dict[str, set[str]] = {}
    for domain, rows in by_domain.items():
        symbols: set[str] = set()
        for rec in rows:
            for sym in rec.get("candidate_symbols") or []:
                if isinstance(sym, str) and sym:
                    symbols.add(sym)
        domain_symbol_sets[domain] = symbols

    scores: list[SourceScore] = []
    for domain, rows in by_domain.items():
        if len(rows) < min_records:
            continue

        record_count = len(rows)
        relevant_rows = [r for r in rows if bool(r.get("is_finance_relevant"))]
        relevant_count = len(relevant_rows)
        relevant_rate = relevant_count / record_count

        if relevant_rows:
            mean_relevance = sum(
                float(r.get("finance_relevance_score") or 0.0) for r in relevant_rows
            ) / len(relevant_rows)
        else:
            mean_relevance = 0.0

        # signal_density uses ALL rows (high-relevance content vs total volume).
        signal_density = sum(
            1 for r in rows
            if float(r.get("finance_relevance_score") or 0.0) >= 0.7
        ) / record_count

        # sentiment_skew over rows with a labeled sentiment.
        sent_counts = Counter(
            r.get("sentiment_label")
            for r in rows
            if r.get("sentiment_label") in ("positive", "neutral", "negative")
        )
        sent_total = sum(sent_counts.values())
        if sent_total > 0:
            sentiment_skew = (
                sent_counts.get("positive", 0) - sent_counts.get("negative", 0)
            ) / sent_total
        else:
            sentiment_skew = 0.0

        # diversity: flatten the list-valued fields across rows.
        all_assets: list[str] = []
        all_reasons: list[str] = []
        for r in rows:
            all_assets.extend(r.get("asset_classes") or [])
            all_reasons.extend(r.get("impact_reason_codes") or [])
        asset_diversity = _normalized_entropy(all_assets)
        reason_diversity = _normalized_entropy(all_reasons)

        # symbol_uniqueness: this domain's symbols not seen elsewhere.
        my_symbols = domain_symbol_sets[domain]
        if my_symbols:
            others: set[str] = set()
            for other_d, other_syms in domain_symbol_sets.items():
                if other_d != domain:
                    others |= other_syms
            unique = my_symbols - others
            symbol_uniqueness = len(unique) / len(my_symbols)
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
