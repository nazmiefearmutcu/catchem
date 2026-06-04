"""Global news tone signal — GDELT DOC 2.0 ``TimelineTone`` lens.

GDELT (the Global Database of Events, Language, and Tone) re-indexes the
world's broadcast/print/web news every ~15 minutes and exposes a free,
no-auth ``mode=TimelineTone`` timeline: the *average article tone* for a
query over time. Tone is GDELT's signed sentiment score — roughly the
percentage of positive minus negative emotional words in matching coverage.
Typical values sit in the ``[-10, +10]`` band; negative = anxious/negative
press, positive = optimistic/positive press, ``0`` ≈ neutral.

This module turns that raw timeline into an *extracted* quant signal — a
compact tone summary (latest / mean / trend / state) per economic theme —
rather than re-emitting headlines. It is the macro-sentiment complement to
the per-record signals: where ``sentiment_momentum`` reads catchem's own
ingested corpus, ``global_tone`` reads the entire global press firehose.

Three public entry points:

  * :func:`summarize_tone` — pure + deterministic. Given a list of GDELT
    ``TimelineTone`` points it computes latest/mean/min/max, a recent-window
    trend (slope + delta), and a ``tone_state`` classification. Injectable
    ``now`` for reproducible tests; tolerant of malformed points.
  * :func:`fetch_tone` — async. GETs the DOC 2.0 ``TimelineTone`` endpoint
    for one query and returns the parsed ``data`` array. Fail-soft → ``[]``.
  * :func:`compute_global_tone` — orchestrator. Fans out over a handful of
    economic themes, summarizes each, and rolls them up into an overall
    tone + state.

Design priorities mirror the rest of the quant package: pure where it can
be, fail-soft everywhere else, stdlib-only for the math.
"""

from __future__ import annotations

import functools
import math
import statistics
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

__all__ = [
    "DEFAULT_THEMES",
    "compute_global_tone",
    "fetch_tone",
    "summarize_tone",
]

# DOC 2.0 endpoint — same host the ArtList source pack uses, different mode.
_GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT's timeline date stamp, e.g. "20260528T143000Z". The TimelineTone
# series uses the same ``YYYYMMDDTHHMMSSZ`` UTC stamp as ArtList's seendate.
_GDELT_TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Per-request HTTP timeout — only used when the caller does NOT inject a
# client (compute_global_tone opens its own short-lived AsyncClient). Kept
# in step with news_poller's connect/read budget so a slow GDELT edge can't
# wedge the endpoint.
_FETCH_TIMEOUT_SECONDS = 8.0

# A tone delta (latest-window mean minus earlier-window mean) whose absolute
# value is below this is "stable". GDELT tone typically lives in [-10, +10],
# so a 0.5-point average swing is a meaningful regime nudge without being so
# tight that noise trips it every poll.
_STATE_THRESHOLD = 0.5

# Fraction of the series treated as the "recent" window for trend/slope.
# 0.4 → the most recent ~40% of points form the recent window, the earlier
# ~60% the baseline. Clamped to at least one point on each side below.
_RECENT_WINDOW_FRACTION = 0.4

# The economic themes the orchestrator fetches by default. Name → GDELT
# query string (boolean OR groups are valid DOC 2.0 grammar; fetch_tone
# URL-encodes them). Kept small so a single dashboard refresh fans out to
# only four outbound GETs.
DEFAULT_THEMES: dict[str, str] = {
    "markets": "stock market",
    "economy": "economy OR recession",
    "crypto": "bitcoin OR crypto",
    "fed": "federal reserve OR inflation",
}


def _parse_point_date(value: Any) -> datetime | None:
    """Parse one GDELT timeline ``date`` into a tz-aware UTC datetime.

    Accepts the canonical ``YYYYMMDDTHHMMSSZ`` string, a plain ISO-8601
    string (defensive — some GDELT modes vary), or an epoch number
    (int/float seconds). Returns ``None`` for anything unparseable so the
    caller can drop the point without exception handling.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        return _parse_point_date_cached(value)
    return None


@functools.lru_cache(maxsize=2048)
def _parse_point_date_cached(value: int | float | str) -> datetime | None:
    # Epoch seconds (int or float).
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    raw = value.strip()
    if not raw:
        return None
    # Canonical GDELT stamp first.
    if len(raw) == 16 and raw[8] == "T" and raw[15] == "Z":
        try:
            return datetime(
                int(raw[0:4]),
                int(raw[4:6]),
                int(raw[6:8]),
                int(raw[9:11]),
                int(raw[11:13]),
                int(raw[13:15]),
                tzinfo=UTC,
            )
        except ValueError:
            pass
    try:
        return datetime.strptime(raw, _GDELT_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        pass
    # All-digit string → epoch seconds.
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    # Last resort: ISO-8601 (handles a trailing Z).
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    if parsed.tzinfo == UTC:
        return parsed
    return parsed.astimezone(UTC)


def _coerce_value(value: Any) -> float | None:
    """Coerce a point's ``value`` to a finite float, else ``None``.

    GDELT sends tone as a JSON number, but defensive callers may hand us a
    numeric string. ``bool`` is rejected explicitly (``True`` is an int in
    Python and would silently read as tone ``1.0``).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    # Drop NaN / ±inf — they'd poison mean/min/max.
    if not math.isfinite(f):
        return None
    return f


def _clean_points(timeline: Any) -> list[tuple[datetime | None, float]]:
    """Filter a raw timeline into ``(parsed_date_or_None, value)`` tuples.

    A point survives if it is a dict with a coercible numeric ``value``.
    The date is parsed best-effort: a point with a good value but an
    unparseable date still counts toward mean/min/max (date only matters
    for ordering the trend window), so its date slot is ``None``.
    Non-dict / value-less points are dropped entirely.
    """
    if not isinstance(timeline, list):
        return []
    out: list[tuple[datetime | None, float]] = []
    for pt in timeline:
        if not isinstance(pt, dict):
            continue
        val = _coerce_value(pt.get("value"))
        if val is None:
            continue
        out.append((_parse_point_date(pt.get("date")), val))
    return out


def _empty_summary(now: datetime) -> dict[str, Any]:
    """Zeroed summary for an empty/all-malformed timeline."""
    return {
        "latest_tone": None,
        "mean_tone": None,
        "min_tone": None,
        "max_tone": None,
        "tone_trend": 0.0,
        "tone_slope": 0.0,
        "tone_state": "stable",
        "n_points": 0,
        "n_dated_points": 0,
        "generated_at": now.isoformat(),
    }


def summarize_tone(
    timeline: list[dict],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize a GDELT ``TimelineTone`` series into a tone signal.

    Parameters
    ----------
    timeline:
        List of points shaped ``{"date": <YYYYMMDDTHHMMSSZ | epoch>,
        "value": <avg tone float>}``. Malformed points (non-dict, missing
        or non-numeric ``value``) are dropped silently; a point with a bad
        ``date`` but a good ``value`` still counts toward the aggregates.
    now:
        Injected wall-clock for ``generated_at`` (deterministic tests).
        Defaults to ``datetime.now(UTC)``.

    Returns
    -------
    dict
        ``latest_tone`` — most recent point's tone (chronological last).
        ``mean_tone``   — arithmetic mean of all valid points.
        ``min_tone`` / ``max_tone`` — range of valid points.
        ``tone_trend``  — recent-window mean minus earlier-window mean
                          (signed; >0 = improving press, <0 = worsening).
        ``tone_slope``  — least-squares slope per point index across the
                          whole series (amplitude-free trend direction).
        ``tone_state``  — ``"improving" | "deteriorating" | "stable"`` keyed
                          on ``tone_trend`` vs ``±_STATE_THRESHOLD``.
        ``n_points``    — count of valid points (dated + undated).
        ``n_dated_points``— count of points backing the ordered trend/latest
                          sequence (dated points only, or all points when
                          none are dated). ``tone_trend`` is only meaningful
                          when this is ``>= 2``.
        ``generated_at``— ISO timestamp from ``now``.

    Never raises on bad input — an empty or all-malformed timeline yields a
    neutral all-zero summary with ``tone_state="stable"``.
    """
    resolved_now = now or datetime.now(UTC)
    points = _clean_points(timeline)
    if not points:
        return _empty_summary(resolved_now)

    # Order-FREE aggregates see every valid point (a point with a bad date but
    # a good value still counts toward mean/min/max — its time only matters for
    # ordering, not for the range).
    all_values = [v for _d, v in points]
    n = len(all_values)
    mean_tone = statistics.fmean(all_values)
    min_tone = min(all_values)
    max_tone = max(all_values)

    # Order-SENSITIVE stats (latest / trend / slope) run over a chronological
    # sequence built from DATED points only, sorted oldest→newest. A point with
    # an unparseable date has no defensible position, so trusting it as "latest"
    # or letting it anchor the trend window would be wrong. Only when NO point
    # carries a usable date do we fall back to the raw order GDELT sent
    # (oldest→newest) so a fully date-less series still yields a latest/trend.
    dated = sorted((d, v) for d, v in points if d is not None)
    ordered = [v for _d, v in dated] if dated else all_values
    m = len(ordered)

    latest_tone = ordered[-1]

    # Recent-window vs earlier-window delta. With a single ordered point there
    # is no earlier window, so the trend is 0 and the state is stable.
    if m >= 2:
        recent_count = max(1, round(m * _RECENT_WINDOW_FRACTION))
        # Guarantee at least one point in the earlier window too.
        recent_count = min(recent_count, m - 1)
        earlier = ordered[: m - recent_count]
        recent = ordered[m - recent_count :]
        tone_trend = statistics.fmean(recent) - statistics.fmean(earlier)
    else:
        tone_trend = 0.0

    tone_slope = _least_squares_slope(ordered)

    if tone_trend > _STATE_THRESHOLD:
        tone_state = "improving"
    elif tone_trend < -_STATE_THRESHOLD:
        tone_state = "deteriorating"
    else:
        tone_state = "stable"

    return {
        "latest_tone": round(latest_tone, 4),
        "mean_tone": round(mean_tone, 4),
        "min_tone": round(min_tone, 4),
        "max_tone": round(max_tone, 4),
        "tone_trend": round(tone_trend, 4),
        "tone_slope": round(tone_slope, 6),
        "tone_state": tone_state,
        "n_points": n,
        # Count of points that produced the ordered (trend/latest) sequence —
        # dated points only (or all points when none are dated, matching the
        # fallback above). tone_trend is only meaningful when this is >= 2;
        # compute_global_tone filters on it so a series with many undated
        # points but < 2 dated points doesn't fold a spurious 0.0 trend into
        # the overall rollup.
        "n_dated_points": m,
        "generated_at": resolved_now.isoformat(),
    }


def _least_squares_slope(values: list[float]) -> float:
    """Ordinary least-squares slope of ``values`` against index 0..n-1.

    Returns 0.0 for fewer than two points or a degenerate (zero-variance)
    x — which can't happen for a contiguous index but is guarded anyway.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = statistics.fmean(values)
    num = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(values))
    den = n * (n * n - 1) / 12.0
    return num / den


def _extract_timeline_data(payload: Any) -> list[dict]:
    """Pull the ``data`` array out of a DOC 2.0 TimelineTone envelope.

    Shape: ``{"timeline": [{"series": "...", "data": [{"date","value"},
    ...]}, ...]}``. TimelineTone returns a single series; we take the first
    series' ``data``. Tolerant: any unexpected shape yields ``[]``.
    """
    if not isinstance(payload, dict):
        return []
    timeline = payload.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return []
    first = timeline[0]
    if not isinstance(first, dict):
        return []
    data = first.get("data")
    if not isinstance(data, list):
        return []
    # Keep only dict points; downstream summarize_tone re-validates anyway.
    return [pt for pt in data if isinstance(pt, dict)]


def _build_tone_url(query: str, *, timespan: str) -> str:
    """Assemble a DOC 2.0 ``TimelineTone`` URL with the query URL-encoded."""
    return (
        f"{_GDELT_DOC_API}"
        f"?query={quote_plus(query)}"
        "&mode=TimelineTone"
        "&format=json"
        f"&timespan={quote_plus(timespan)}"
    )


async def fetch_tone(
    client: Any,
    query: str,
    *,
    timespan: str = "1d",
) -> list[dict]:
    """Fetch one query's GDELT ``TimelineTone`` series.

    GETs the DOC 2.0 endpoint with ``mode=TimelineTone&format=json`` and
    returns the parsed ``data`` array (``[{"date","value"}, ...]``).

    Parameters
    ----------
    client:
        An object exposing an async ``get(url)`` returning a response with
        ``.json()`` (``httpx.AsyncClient`` in production; a mock in tests).
    query:
        GDELT query string (un-encoded; this function encodes it).
    timespan:
        GDELT relative window, e.g. ``"1d"``, ``"3d"``, ``"1w"``.

    Fail-soft: any transport error, non-JSON body, or unexpected shape
    yields ``[]`` so a single dead theme never breaks the orchestrator.
    """
    url = _build_tone_url(query, timespan=timespan)
    try:
        response = await client.get(url)
        payload = response.json()
    except Exception:
        return []
    return _extract_timeline_data(payload)


async def compute_global_tone(
    themes: dict[str, str] | None = None,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Fetch + summarize global news tone across a set of economic themes.

    For each ``{name: query}`` it fetches the GDELT ``TimelineTone`` series
    and runs :func:`summarize_tone`, then rolls the per-theme latest tones
    into an ``overall_tone`` (mean of available latests) and an
    ``overall_state``.

    Parameters
    ----------
    themes:
        Mapping of theme name → GDELT query. Defaults to
        :data:`DEFAULT_THEMES` (markets / economy / crypto / fed).
    client:
        Optional async HTTP client. When omitted a short-lived
        ``httpx.AsyncClient`` is opened and closed here. Injecting a client
        lets the caller reuse a pooled connection (and lets tests pass a
        mock without touching the network).

    Returns
    -------
    dict
        ``generated_at`` — ISO timestamp.
        ``by_theme``     — ``{name: summarize_tone(...)}`` for each theme.
        ``overall_tone`` — mean of the per-theme ``latest_tone`` values that
                           are present (``None`` when every theme is empty).
        ``overall_state``— state classification of ``overall_tone`` derived
                           from the mean of per-theme ``tone_trend`` values.

    Fail-soft throughout: a theme whose fetch returns ``[]`` still appears
    in ``by_theme`` with its neutral empty summary.
    """
    resolved_themes = themes if themes is not None else DEFAULT_THEMES
    now = datetime.now(UTC)

    owns_client = client is None
    if owns_client:
        import httpx

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(_FETCH_TIMEOUT_SECONDS),
            headers={"User-Agent": "catchem-global-tone/1.0"},
        )
    try:
        by_theme: dict[str, dict[str, Any]] = {}
        for name, query in resolved_themes.items():
            timeline = await fetch_tone(client, query)
            by_theme[name] = summarize_tone(timeline, now=now)
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:
                pass

    latests = [s["latest_tone"] for s in by_theme.values() if s.get("latest_tone") is not None]
    trends = [
        s["tone_trend"]
        for s in by_theme.values()
        # Gate on the count of points that actually produced the trend
        # (dated points only). A theme with many undated points but < 2
        # dated points has a meaningless 0.0 tone_trend that must not be
        # averaged into the overall rollup.
        if s.get("n_dated_points", 0) >= 2
    ]

    overall_tone = round(statistics.fmean(latests), 4) if latests else None
    mean_trend = statistics.fmean(trends) if trends else 0.0
    if mean_trend > _STATE_THRESHOLD:
        overall_state = "improving"
    elif mean_trend < -_STATE_THRESHOLD:
        overall_state = "deteriorating"
    else:
        overall_state = "stable"

    return {
        "generated_at": now.isoformat(),
        "by_theme": by_theme,
        "overall_tone": overall_tone,
        "overall_state": overall_state,
    }
