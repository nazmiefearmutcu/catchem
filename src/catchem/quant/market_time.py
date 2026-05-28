"""Market time-of-day signal.

Buckets news arrivals into US equity market sessions (NYSE schedule,
eastern time) and computes relevance/volume per bucket.

Sessions (US eastern):
  * ``pre_open``    04:00 - 09:30
  * ``open``        09:30 - 11:00  (high-volatility opening hour)
  * ``lunch``       11:00 - 14:00
  * ``close``       14:00 - 16:30  (closing-hour push)
  * ``after_hours`` 16:30 - 20:00
  * ``overnight``   20:00 - 04:00 (next day)
  * ``weekend``     Saturday all day + Sunday before 18:00 ET

Design notes
------------
* DST handled by ``zoneinfo`` (stdlib, no third-party tz).
* Records with missing / malformed ``published_ts`` (or ``created_at``)
  are silently skipped — no exceptions propagate to the signal layer.
* Same input → same output. Read-only against catchem storage.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Canonical bucket order — also defines the output ordering.
SESSIONS: tuple[str, ...] = (
    "pre_open",
    "open",
    "lunch",
    "close",
    "after_hours",
    "overnight",
    "weekend",
)


@dataclass(frozen=True)
class SessionBucket:
    """Aggregated stats for one market session bucket."""

    session: str
    volume: int
    avg_score: float
    relevant_count: int


def classify_session(timestamp: datetime) -> str:
    """Classify a UTC-aware ``datetime`` into a market session label.

    The input must be timezone-aware; convert it to eastern time, then
    apply the session rules above.
    """
    et = timestamp.astimezone(ET)
    weekday = et.weekday()  # Monday=0 .. Sunday=6

    # Weekend rules: full Saturday, Sunday before 18:00 ET (the futures
    # market is closed and news flow is non-market).
    if weekday == 5:
        return "weekend"
    if weekday == 6 and et.hour < 18:
        return "weekend"

    minutes = et.hour * 60 + et.minute
    if minutes < 4 * 60:
        return "overnight"  # 00:00 - 04:00
    if minutes < 9 * 60 + 30:
        return "pre_open"  # 04:00 - 09:30
    if minutes < 11 * 60:
        return "open"  # 09:30 - 11:00
    if minutes < 14 * 60:
        return "lunch"  # 11:00 - 14:00
    if minutes < 16 * 60 + 30:
        return "close"  # 14:00 - 16:30
    if minutes < 20 * 60:
        return "after_hours"  # 16:30 - 20:00
    return "overnight"  # 20:00 onwards


def _parse_ts(value: Any) -> datetime | None:
    """Parse a record timestamp string; return ``None`` on any error.

    Accepts both ``2026-05-27T12:34:56Z`` and ``+00:00`` forms.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        return None
    return ts


def aggregate_by_session(records: list[dict]) -> list[SessionBucket]:
    """Group ``records`` by market session and compute volume / score stats.

    Always returns one bucket per canonical session, in order — even when
    the bucket is empty (volume=0, avg_score=0.0, relevant_count=0). The
    UI can rely on a stable ordering for the bar chart.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)

    for r in records:
        ts = _parse_ts(r.get("published_ts")) or _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        session = classify_session(ts)
        buckets[session].append(r)

    result: list[SessionBucket] = []
    for session in SESSIONS:
        items = buckets.get(session, [])
        if not items:
            result.append(
                SessionBucket(session=session, volume=0, avg_score=0.0, relevant_count=0)
            )
            continue
        scores: list[float] = []
        for r in items:
            raw = r.get("finance_relevance_score")
            if raw is None:
                scores.append(0.0)
                continue
            try:
                scores.append(float(raw))
            except (TypeError, ValueError):
                scores.append(0.0)
        relevant = sum(1 for s in scores if s >= 0.5)
        avg = sum(scores) / len(scores) if scores else 0.0
        result.append(
            SessionBucket(
                session=session,
                volume=len(items),
                avg_score=avg,
                relevant_count=relevant,
            )
        )
    return result
