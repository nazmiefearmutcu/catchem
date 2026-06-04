"""Hacker News (Algolia) full-text search source pack.

Hacker News ships an official RSS feed, but it's a single global firehose
capped at the front page — fine for breadth, poor for *finance* recall and
latency. The Algolia-powered HN Search API is both richer and more
real-time: it exposes per-query full-text search over every story as plain
JSON with no auth, and `search_by_date` returns the freshest matching
stories newest-first. That lets us target the finance/markets/economy/crypto
vocabulary directly, so the frequent fintech and market threads on HN surface
in the Live Feed quickly instead of waiting to crest the front page.

This pack:
  * registers a ``"hn_algolia"`` body parser (`_parse_hn`) that turns an
    Algolia ``{"hits": [...]}`` envelope into ParsedItems, and
  * registers a feed provider contributing ~6 finance-relevant
    `search_by_date` queries.

HN search hits carry no article body — only a headline — so the title is
mapped into both ``title`` and ``text``. Timestamps come from Algolia's
``created_at_i`` epoch integer (preferred) or the ISO ``created_at`` string,
either way normalized to a timezone-aware UTC datetime, with a ``now(UTC)``
fallback. A hit whose own ``url``/``story_url`` is absent (an Ask HN / text
post) still links to its HN discussion page via ``objectID``. Everything is
defensive: a malformed envelope yields ``[]`` rather than raising, and hits
with no usable title are skipped.
"""

from __future__ import annotations

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

# Algolia HN Search, "by date" endpoint → newest-first matching stories.
# `tags=story` restricts to submissions (drops comments/polls); a healthy
# per-query page keeps each tick's recall high. Dedup absorbs the inevitable
# overlap across polls.
_HN_ALGOLIA_API = "https://hn.algolia.com/api/v1/search_by_date"

# Canonical HN discussion-page template — used when a hit has no external URL.
_HN_ITEM_URL = "https://news.ycombinator.com/item?id={object_id}"

# Domain to attribute when a hit links to its own HN discussion page (or when
# a URL host can't be parsed).
_HN_DOMAIN = "news.ycombinator.com"

# Finance-relevant query → feed-slug pairs. Boolean OR groups are part of
# Algolia's query grammar; quote_plus URL-encodes the spaces and any operators
# so the assembled URL is valid.
_QUERIES: tuple[tuple[str, str], ...] = (
    ("stocks", "stocks"),
    ("markets-economy", "markets OR economy"),
    ("bitcoin-crypto", "bitcoin OR crypto"),
    ("fed-inflation", "Federal Reserve OR inflation"),
    ("earnings-ipo", "earnings OR IPO"),
    ("recession-layoffs", "recession OR layoffs"),
)


def _build_url(query: str) -> str:
    """Assemble a `search_by_date` URL with the query properly URL-encoded."""
    return f"{_HN_ALGOLIA_API}?query={quote_plus(query)}&tags=story&hitsPerPage=50"


@lru_cache(maxsize=2048)
def _parse_epoch_cached(epoch: int | float) -> datetime | None:
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@lru_cache(maxsize=2048)
def _parse_iso_cached(raw: str) -> datetime | None:
    val_len = len(raw)
    if (
        val_len == 20
        and raw[4] == "-"
        and raw[7] == "-"
        and raw[10] == "T"
        and raw[13] == ":"
        and raw[16] == ":"
        and raw[19] == "Z"
    ):
        try:
            return datetime(
                int(raw[0:4]),
                int(raw[5:7]),
                int(raw[8:10]),
                int(raw[11:13]),
                int(raw[14:16]),
                int(raw[17:19]),
                tzinfo=UTC,
            )
        except ValueError:
            pass

    if (
        val_len == 24
        and raw[4] == "-"
        and raw[7] == "-"
        and raw[10] == "T"
        and raw[13] == ":"
        and raw[16] == ":"
        and raw[19] == "."
        and raw[23] == "Z"
    ):
        try:
            return datetime(
                int(raw[0:4]),
                int(raw[5:7]),
                int(raw[8:10]),
                int(raw[11:13]),
                int(raw[14:16]),
                int(raw[17:19]),
                int(raw[20:23]) * 1000,
                tzinfo=UTC,
            )
        except ValueError:
            pass

    normalized = raw
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_created(hit: dict[str, object]) -> datetime:
    """Resolve a hit's publish time → tz-aware UTC datetime.

    Prefers the ``created_at_i`` epoch integer; falls back to parsing the ISO
    ``created_at`` string; finally falls back to ``now(UTC)`` so a single odd
    record never poisons the whole batch.
    """
    epoch = hit.get("created_at_i")
    if isinstance(epoch, bool):
        epoch = None  # guard: bool is an int subclass
    if isinstance(epoch, (int, float)):
        dt = _parse_epoch_cached(epoch)
        if dt is not None:
            return dt

    iso = hit.get("created_at")
    if isinstance(iso, str):
        raw = iso.strip()
        if raw:
            dt = _parse_iso_cached(raw)
            if dt is not None:
                return dt

    return datetime.now(UTC)


def _resolve_domain(url: str, *, is_item_page: bool) -> str:
    """Derive the display domain from the (external) URL host.

    HN discussion-page links always attribute to ``news.ycombinator.com``;
    external links use their own host (with a leading ``www.`` stripped),
    falling back to the HN domain if the host can't be parsed.
    """
    if is_item_page:
        return _HN_DOMAIN
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    if not host:
        return _HN_DOMAIN
    return host[4:] if host.startswith("www.") else host


def _parse_hn(body: bytes, fallback_domain: str) -> list[ParsedItem]:
    """Parse an Algolia HN Search JSON body into a list[ParsedItem].

    Shape: ``{"hits": [{"title"/"story_title", "url"/"story_url",
    "objectID", "created_at_i", "created_at", ...}, ...]}``.

    HN search carries no article body, so the headline becomes both title and
    text. A hit with no external ``url``/``story_url`` links to its HN
    discussion page via ``objectID``. Tolerant: any decode/shape error returns
    ``[]``; hits with no usable title are skipped.
    """
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return []
        hits = payload.get("hits")
        if not isinstance(hits, list):
            return []

        items: list[ParsedItem] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue

            title_raw = hit.get("title")
            if not isinstance(title_raw, str) or not title_raw.strip():
                title_raw = hit.get("story_title")
            title = title_raw.strip() if isinstance(title_raw, str) else ""
            if not title:
                continue  # no headline → nothing useful to surface; skip

            url_raw = hit.get("url")
            if not isinstance(url_raw, str) or not url_raw.strip():
                url_raw = hit.get("story_url")
            url = url_raw.strip() if isinstance(url_raw, str) else ""

            is_item_page = False
            if not url:
                object_id = hit.get("objectID")
                if object_id is None or (isinstance(object_id, str) and not object_id.strip()):
                    continue  # no URL and no id → can't link or dedup; skip
                url = _HN_ITEM_URL.format(object_id=str(object_id).strip())
                is_item_page = True

            domain = _resolve_domain(url, is_item_page=is_item_page) or fallback_domain
            items.append(
                ParsedItem(
                    title=title,
                    text=title,  # HN search carries no body — reuse the headline.
                    url=url,
                    domain=domain,
                    published_ts=_parse_created(hit),
                )
            )
        return items
    except Exception:
        return []


# Register the parser at import time so `get_parser("hn_algolia")` resolves
# once this module is auto-discovered by catchem.news_sources.
register_parser("hn_algolia", _parse_hn)


@register_feed_provider
def _hn_algolia_feeds() -> list[FeedSpec]:
    """Contribute the finance-relevant HN Algolia search_by_date queries."""
    return [
        FeedSpec(
            name=f"hn-algolia-{slug}",
            url=_build_url(query),
            fallback_domain=_HN_DOMAIN,
            parser="hn_algolia",
        )
        for slug, query in _QUERIES
    ]
