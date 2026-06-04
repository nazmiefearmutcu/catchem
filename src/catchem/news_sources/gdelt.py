"""GDELT global-news firehose source pack.

GDELT (the Global Database of Events, Language, and Tone) monitors the
world's broadcast, print, and web news in ~100 languages and re-indexes
every ~15 minutes. Its DOC 2.0 API exposes that firehose as plain JSON
with no auth and a generous record cap, which makes it the single broadest
"aware of ~everything on the internet" source we can plug into the poller.

This pack:
  * registers a `"gdelt"` body parser (`_parse_gdelt`) that turns the DOC
    2.0 JSON envelope into ParsedItems, and
  * registers a feed provider contributing a few finance-relevant DOC 2.0
    queries (markets/economy, crypto, central banks).

GDELT carries no article body — only a headline — so we map the title into
both `title` and `text`. The `seendate` field is GDELT's
``YYYYMMDDTHHMMSSZ`` UTC stamp (e.g. ``20260528T143000Z``); we parse it to a
timezone-aware UTC datetime and fall back to ``now(UTC)`` on any failure.
Everything is defensive: a malformed envelope yields ``[]`` rather than
raising, and articles with no URL are skipped.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import quote_plus, urlparse

from ..news_poller import (
    FeedSpec,
    ParsedItem,
    register_feed_provider,
    register_parser,
)

# DOC 2.0 ArtList endpoint. We ask for the freshest 30-minute window so the
# poller surfaces genuinely real-time hits, JSON output, newest-first, and a
# healthy per-query cap. `timespan=30min` comfortably overlaps the poller's
# 10s-floor cadence; dedup handles the inevitable repeats across ticks.
_GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# The GDELT seendate stamp format, e.g. "20260528T143000Z".
_GDELT_TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Finance-relevant query → feed-name pairs. Boolean OR groups inside parens
# are exactly what GDELT's query grammar expects; quote_plus URL-encodes the
# spaces and parens so the assembled URL is valid.
_QUERIES: tuple[tuple[str, str], ...] = (
    ("gdelt-markets", "(stocks OR markets OR economy)"),
    ("gdelt-crypto", "(bitcoin OR ethereum OR crypto OR cryptocurrency)"),
    ("gdelt-centralbanks", "(federal reserve OR central bank OR interest rates OR inflation)"),
)


def _build_url(query: str) -> str:
    """Assemble a DOC 2.0 ArtList URL with the query properly URL-encoded."""
    return (
        f"{_GDELT_DOC_API}"
        f"?query={quote_plus(query)}"
        "&mode=ArtList"
        "&format=json"
        "&timespan=30min"
        "&maxrecords=75"
        "&sort=DateDesc"
    )


@lru_cache(maxsize=2048)
def _parse_seendate_cached(value: str) -> datetime | None:
    """Cached fast-path parser for GDELT ``YYYYMMDDTHHMMSSZ`` format."""
    if len(value) == 16 and value[8] == "T" and value[15] == "Z":
        try:
            return datetime(
                int(value[0:4]),
                int(value[4:6]),
                int(value[6:8]),
                int(value[9:11]),
                int(value[11:13]),
                int(value[13:15]),
                tzinfo=UTC,
            )
        except ValueError:
            pass
    try:
        return datetime.strptime(value, _GDELT_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_seendate(value: object) -> datetime:
    """Parse GDELT's ``YYYYMMDDTHHMMSSZ`` stamp → tz-aware UTC datetime.

    Falls back to ``now(UTC)`` on missing/malformed input so a single odd
    record never poisons the whole batch.
    """
    if isinstance(value, str) and value:
        parsed = _parse_seendate_cached(value)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def _resolve_domain(article: dict[str, object], url: str, fallback_domain: str) -> str:
    """Prefer GDELT's own `domain` field; else derive from the URL; else fallback."""
    raw = article.get("domain")
    if isinstance(raw, str) and raw.strip():
        host = raw.strip()
    else:
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
    if not host:
        return fallback_domain
    return host[4:] if host.startswith("www.") else host


def _parse_gdelt(body: bytes, fallback_domain: str) -> list[ParsedItem]:
    """Parse a GDELT DOC 2.0 ArtList JSON body into a list[ParsedItem].

    Shape: ``{"articles": [{"url","title","seendate","domain", ...}, ...]}``.
    GDELT has no article body, so the headline becomes both title and text.
    Tolerant: any decode/shape error returns ``[]``; articles missing a URL
    (or a usable title) are skipped.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []

    if not isinstance(payload, dict):
        return []
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []

    items: list[ParsedItem] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        url = article.get("url")
        if not isinstance(url, str) or not url.strip():
            continue  # no URL → can't dedup or link; skip
        url = url.strip()
        title_raw = article.get("title")
        # GDELT embeds the raw page <title> verbatim into the JSON string, so
        # HTML entities (e.g. "&amp;", "&#39;") survive unescaped — JSON does
        # not decode them. Unescape so display text is clean AND the poller's
        # cross-source title-dedup matches the RSS copy (whose entities the XML
        # parser already decoded). Without this, "&amp;"/"&#39;" leak spurious
        # "amp"/"39" tokens into _normalize_title and the dup never collapses.
        title = html.unescape(title_raw).strip() if isinstance(title_raw, str) else ""
        if not title:
            # Title-less GDELT rows are rare; synthesize from the host so the
            # Live Feed row is still readable and the item isn't dropped.
            title = url
        items.append(ParsedItem(
            title=title,
            text=title,  # GDELT carries no body — reuse the headline.
            url=url,
            domain=_resolve_domain(article, url, fallback_domain),
            published_ts=_parse_seendate(article.get("seendate")),
        ))
    return items


# Register the parser at import time so `get_parser("gdelt")` resolves once
# this module is auto-discovered by catchem.news_sources.
register_parser("gdelt", _parse_gdelt)


@register_feed_provider
def _gdelt_feeds() -> list[FeedSpec]:
    """Contribute the finance-relevant GDELT DOC 2.0 firehose queries."""
    return [
        FeedSpec(
            name=name,
            url=_build_url(query),
            fallback_domain="gdelt.org",
            parser="gdelt",
        )
        for name, query in _QUERIES
    ]
