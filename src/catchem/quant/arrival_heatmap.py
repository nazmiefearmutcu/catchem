"""News arrival heatmap — 24h x 7day grid showing volume per (hour, weekday).

Aggregates a rolling window of records into a fixed 168-cell grid indexed
by ``(weekday, hour)``. Useful for:

* Detecting weekend dips and the Sunday-evening futures-reopen ramp
* Spotting the US-equity open / close volume peaks
* Surfacing unusual after-hours bursts the session-clustering signal
  rolls up into a single ``after_hours`` bucket

Design notes
------------
* Defaults to ``America/New_York`` so the grid anchors to the US market
  schedule (matches ``market_time.py``). Callers can pass any tz name.
* Records with malformed / missing ``published_ts`` (or ``created_at``)
  are silently skipped — no exceptions propagate to the signal layer.
* Always returns a dense 24x7 = 168 cell grid in canonical order so the
  UI can render the ECharts heatmap without densifying client-side.
* stdlib zoneinfo only (Python 3.9+ ships it).
"""

from __future__ import annotations

import functools
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

WEEKDAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class HeatmapCell:
    """One cell of the 24x7 grid."""

    weekday: int  # 0 = Monday, 6 = Sunday
    hour: int  # 0..23 (local hour in the requested timezone)
    count: int


@functools.lru_cache(maxsize=8192)
def _parse_ts_cached(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        return None
    return ts


def _parse_ts(value: Any) -> datetime | None:
    """Parse a record timestamp string; return ``None`` on any error."""
    if not isinstance(value, str) or not value:
        return None
    return _parse_ts_cached(value)


@functools.lru_cache(maxsize=8192)
def _get_weekday_hour(ts: datetime, tz: ZoneInfo) -> tuple[int, int]:
    local_ts = ts.astimezone(tz)
    return local_ts.weekday(), local_ts.hour


@functools.lru_cache(maxsize=64)
def _get_zone_info(timezone_name: str) -> tuple[ZoneInfo, str]:
    try:
        tz = ZoneInfo(timezone_name)
        return tz, timezone_name
    except Exception:
        return ET, "America/New_York"


def compute_heatmap(
    records: list[dict],
    timezone: str = "America/New_York",
) -> dict:
    """Return a 24x7 arrival-volume grid.

    Parameters
    ----------
    records:
        Iterable of catchem record dicts. Each must carry either
        ``published_ts`` or ``created_at`` as an ISO-8601 string with
        explicit timezone (``Z`` is normalised to ``+00:00``).
    timezone:
        IANA tz name. Defaults to ET to anchor on the US market schedule.
        Falls back to ET if the name is invalid.

    Returns
    -------
    dict:
        ``cells``         — 168 entries in canonical row-major order
        (weekday outer, hour inner; weekday 0..6 => Mon..Sun).
        ``max_count``     — largest count across all cells (0 if empty).
        ``total_samples`` — sum of cell counts.
        ``peak_cells``    — up to 5 cells tied for ``max_count`` (>0).
        ``timezone``      — the resolved tz name actually used.
        ``weekday_labels``— ``["Mon", ..., "Sun"]`` for UI Y-axis.
    """
    if not isinstance(timezone, str):
        timezone_str = "America/New_York"
    else:
        timezone_str = timezone

    tz, resolved_tz = _get_zone_info(timezone_str)

    grid: dict[tuple[int, int], int] = defaultdict(int)

    for r in records:
        ts = _parse_ts(r.get("published_ts"))
        if ts is None:
            ts = _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        w, h = _get_weekday_hour(ts, tz)
        grid[(w, h)] += 1

    cells = [
        {
            "weekday": weekday,
            "hour": hour,
            "count": grid.get((weekday, hour), 0),
        }
        for weekday in range(7)
        for hour in range(24)
    ]

    max_count = max(grid.values(), default=0)
    peak_cells = [c for c in cells if c["count"] == max_count] if max_count > 0 else []

    return {
        "cells": cells,
        "max_count": max_count,
        "total_samples": sum(grid.values()),
        "peak_cells": peak_cells[:5],
        "timezone": resolved_tz,
        "weekday_labels": list(WEEKDAY_LABELS),
    }
