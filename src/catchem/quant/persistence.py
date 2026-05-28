"""
News persistence signal — long-running stories.

A "persistent narrative" is one whose mentions span many distinct day buckets.
A story that hits the news for 1 hour then dies has low persistence; a story
that gets mentioned across 5+ days has high persistence (geopolitical conflicts,
ongoing trials, central bank decision cycles).

We treat each (asset_class, top_symbol) pair as a story scope. For records
covering that scope, count how many distinct UTC-date buckets received at
least one mention. Persistence ratio = days_covered / window_days.

Outputs:
- Per-scope: days_covered, total_records, persistence_ratio, sample_titles
- Sorted by persistence_ratio desc, then total_records desc.

Why useful:
- Helps the analyst distinguish flash news from structural narratives.
- A high-persistence scope with rising relevance scores often signals
  a regime that will keep moving markets for days/weeks.
- Pairs naturally with sentiment_dispersion: persistent + high-dispersion
  scope = "ongoing debate", persistent + low-dispersion = "unanimous trend".
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class PersistenceBucket:
    scope: str  # e.g. "equities/AAPL" or "rates/—"
    days_covered: int
    total_records: int
    persistence_ratio: float  # 0..1 (days_covered / window_days)
    sample_titles: list[str]


def _parse_day(ts_str: str | None) -> str | None:
    """Return YYYY-MM-DD in UTC from an ISO timestamp; None if unparseable."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return ts.astimezone(UTC).strftime("%Y-%m-%d")


def compute_persistence(
    records: list[dict],
    window_days: int = 7,
    min_records: int = 3,
    top_n: int = 20,
) -> list[PersistenceBucket]:
    """
    Group records by (asset_class, candidate_symbol[0]) scope, count distinct
    day buckets in the trailing `window_days`, return top scopes.

    Records without timestamps or symbols still count toward
    "(asset_class)/—" buckets, so we don't lose data from incomplete records.
    """
    if window_days < 1:
        window_days = 1

    # First find latest day in the corpus
    days: set[str] = set()
    parsed: list[tuple[str, dict]] = []
    for r in records:
        day = _parse_day(r.get("published_ts") or r.get("created_at"))
        if not day:
            continue
        days.add(day)
        parsed.append((day, r))

    if not days:
        return []

    latest_day = max(days)
    latest_ts = datetime.strptime(latest_day, "%Y-%m-%d").replace(tzinfo=UTC)
    cutoff_ts = latest_ts.timestamp() - (window_days - 1) * 86400
    in_window = [
        (day, r) for day, r in parsed
        if datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() >= cutoff_ts
    ]

    # Group by scope = "asset_class/top_symbol"
    scope_days: dict[str, set[str]] = defaultdict(set)
    scope_records: dict[str, list[dict]] = defaultdict(list)
    scope_total: dict[str, int] = defaultdict(int)
    for day, r in in_window:
        asset_classes = r.get("asset_classes") or []
        symbols = r.get("candidate_symbols") or []
        if not isinstance(asset_classes, list):
            asset_classes = [str(asset_classes)] if asset_classes else []
        if not isinstance(symbols, list):
            symbols = [str(symbols)] if symbols else []
        top_sym = symbols[0] if symbols else "—"
        if not asset_classes:
            asset_classes = ["—"]
        for ac in asset_classes:
            scope = f"{ac}/{top_sym}"
            scope_days[scope].add(day)
            scope_total[scope] += 1
            if len(scope_records[scope]) < 3:
                title = r.get("title") or ""
                if title:
                    scope_records[scope].append(title[:100])

    results: list[PersistenceBucket] = []
    for scope, days_set in scope_days.items():
        total = scope_total[scope]
        if total < min_records:
            continue
        ratio = len(days_set) / window_days
        results.append(PersistenceBucket(
            scope=scope,
            days_covered=len(days_set),
            total_records=total,
            persistence_ratio=ratio,
            sample_titles=scope_records[scope],
        ))

    results.sort(key=lambda b: (b.persistence_ratio, b.total_records), reverse=True)
    return results[:top_n]
