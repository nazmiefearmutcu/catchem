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

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class PersistenceBucket:
    scope: str  # e.g. "equities/AAPL" or "rates/—"
    days_covered: int
    total_records: int
    persistence_ratio: float  # 0..1 (days_covered / window_days)
    sample_titles: list[str]


@lru_cache(maxsize=1024)
def _parse_day_cached(ts_str: str) -> str | None:
    try:
        from catchem.storage import _parse_iso_ts_cached
        ts = _parse_iso_ts_cached(ts_str)
    except Exception:
        return None
    ts_utc = ts.astimezone(UTC)
    return f"{ts_utc.year:04d}-{ts_utc.month:02d}-{ts_utc.day:02d}"



def _parse_day(ts_str: str | None) -> str | None:
    """Return YYYY-MM-DD in UTC from an ISO timestamp; None if unparseable."""
    if type(ts_str) is not str:
        return None
    return _parse_day_cached(ts_str)


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
        ts_str = r.get("published_ts") or r.get("created_at")
        day = _parse_day(ts_str)
        if day:
            days.add(day)
            parsed.append((day, r))

    if not days:
        return []

    latest_day = max(days)
    latest_ts = datetime.fromisoformat(latest_day).replace(tzinfo=UTC)
    cutoff_ts = latest_ts.timestamp() - (window_days - 1) * 86400
    cutoff_day = datetime.fromtimestamp(cutoff_ts, tz=UTC).strftime("%Y-%m-%d")

    # Group by scope = "asset_class/top_symbol"
    # scope -> [days_set, total_count, sample_titles_list]
    scopes: dict[str, list] = {}
    for day, r in parsed:
        if day >= cutoff_day:
            asset_classes = r.get("asset_classes")
            if type(asset_classes) is list:
                if not asset_classes:
                    asset_classes = ["—"]
            elif asset_classes:
                asset_classes = [str(asset_classes)]
            else:
                asset_classes = ["—"]

            symbols = r.get("candidate_symbols")
            if type(symbols) is list:
                top_sym = symbols[0] if symbols else "—"
            elif symbols:
                top_sym = str(symbols)
            else:
                top_sym = "—"

            for ac in asset_classes:
                scope = f"{ac}/{top_sym}"
                state = scopes.get(scope)
                if state is None:
                    state = [set(), 0, []]
                    scopes[scope] = state
                state[0].add(day)
                state[1] += 1
                if len(state[2]) < 3:
                    title = r.get("title")
                    if title:
                        state[2].append(title[:100] if type(title) is str else str(title)[:100])

    results: list[PersistenceBucket] = []
    for scope, state in scopes.items():
        total = state[1]
        if total < min_records:
            continue
        days_set = state[0]
        ratio = len(days_set) / window_days
        results.append(
            PersistenceBucket(
                scope=scope,
                days_covered=len(days_set),
                total_records=total,
                persistence_ratio=ratio,
                sample_titles=state[2],
            )
        )

    results.sort(key=lambda b: (b.persistence_ratio, b.total_records), reverse=True)
    return results[:top_n]
