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

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
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


@lru_cache(maxsize=1024)
def classify_session(timestamp: datetime) -> str:
    """Classify a UTC-aware ``datetime`` into a market session label.

    The input must be timezone-aware; convert it to eastern time, then
    apply the session rules above.
    """
    et = timestamp.astimezone(ET)
    weekday = et.weekday()  # Monday=0 .. Sunday=6

    # Weekend rules: full Saturday, Sunday before 18:00 ET (the futures
    # market is closed and news flow is non-market).
    if weekday >= 5:
        if weekday == 5 or et.hour < 18:
            return "weekend"

    minutes = et.hour * 60 + et.minute
    if minutes < 240:
        return "overnight"  # 00:00 - 04:00
    if minutes < 570:
        return "pre_open"  # 04:00 - 09:30
    if minutes < 660:
        return "open"  # 09:30 - 11:00
    if minutes < 840:
        return "lunch"  # 11:00 - 14:00
    if minutes < 990:
        return "close"  # 14:00 - 16:30
    if minutes < 1200:
        return "after_hours"  # 16:30 - 20:00
    return "overnight"  # 20:00 onwards


@lru_cache(maxsize=1024)
def _parse_ts_cached(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        return None
    return ts


def _parse_ts(value: Any) -> datetime | None:
    """Parse a record timestamp string; return ``None`` on any error.

    Accepts both ``2026-05-27T12:34:56Z`` and ``+00:00`` forms.
    """
    if type(value) is not str or not value:
        return None
    return _parse_ts_cached(value)


def aggregate_by_session(records: list[dict]) -> list[SessionBucket]:
    """Group ``records`` by market session and compute volume / score stats.

    Always returns one bucket per canonical session, in order — even when
    the bucket is empty (volume=0, avg_score=0.0, relevant_count=0). The
    UI can rely on a stable ordering for the bar chart.
    """
    volumes = {s: 0 for s in SESSIONS}
    total_scores = {s: 0.0 for s in SESSIONS}
    relevant_counts = {s: 0 for s in SESSIONS}

    for r in records:
        ts = _parse_ts(r.get("published_ts")) or _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        session = classify_session(ts)

        volumes[session] += 1

        raw = r.get("finance_relevance_score")
        if raw is not None:
            t = type(raw)
            if t is float:
                val = raw
            elif t is int:
                val = float(raw)
            elif isinstance(raw, str):
                try:
                    val = float(raw)
                except ValueError:
                    val = 0.0
            else:
                val = 0.0

            total_scores[session] += val
            if val >= 0.5:
                relevant_counts[session] += 1

    result: list[SessionBucket] = []
    for session in SESSIONS:
        vol = volumes[session]
        if vol == 0:
            result.append(SessionBucket(session=session, volume=0, avg_score=0.0, relevant_count=0))
        else:
            result.append(
                SessionBucket(
                    session=session,
                    volume=vol,
                    avg_score=total_scores[session] / vol,
                    relevant_count=relevant_counts[session],
                )
            )
    return result
