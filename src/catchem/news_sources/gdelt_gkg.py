"""GDELT GKG-style theme-targeted global-news source pack.

The base ``gdelt`` pack hits GDELT DOC 2.0 with broad keyword groups
(markets/economy, crypto, central banks). This sibling pack instead leans on
GDELT's **Global Knowledge Graph (GKG)** theme taxonomy — the same machine
codes GDELT assigns when it reads each article and tags its economic themes
and tone. Querying by ``theme:`` codes rather than free-text surfaces stories
GKG has already classified into a financial bucket, which is both broader
(catches non-English / paraphrased coverage the keyword query misses) and
more precise (every hit is on-theme by construction).

Mechanically it shares the DOC 2.0 ArtList JSON envelope with the base pack,
so the body parser is identical — we import and reuse ``_parse_gdelt`` from
``.gdelt`` and simply register it under a distinct ``"gdelt_gkg"`` key. The
feed provider contributes one DOC 2.0 query per economic theme, each named
``gdelt-gkg-<slug>`` so the names never collide with the base pack's
``gdelt-<x>`` feeds in ``assemble_feeds()``.

GDELT carries no article body — only a headline — so (via the shared parser)
the title is mapped into both ``title`` and ``text``. The ``seendate`` field
is GDELT's ``YYYYMMDDTHHMMSSZ`` UTC stamp, parsed to a timezone-aware UTC
datetime with a ``now(UTC)`` fallback. Everything is defensive: a malformed
envelope yields ``[]`` rather than raising, and url-less articles are skipped.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..news_poller import (
    FeedSpec,
    ParsedItem,
    register_feed_provider,
    register_parser,
)

# Reuse the base pack's DOC 2.0 ArtList parser verbatim — the envelope shape
# ({"articles":[{url,title,seendate,domain,...}]}) is the same regardless of
# whether the query was keyword- or theme-based. Importing keeps the two
# packs from drifting on parse semantics (tz handling, url-less skip, etc.).
from .gdelt import _parse_gdelt

# DOC 2.0 ArtList endpoint (same host as the base pack).
_GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"


def _parse_gkg(body: bytes, fallback_domain: str) -> list[ParsedItem]:
    """Parse a GDELT DOC 2.0 ArtList JSON body into a list[ParsedItem].

    Thin alias over the base pack's ``_parse_gdelt`` (theme-targeted queries
    return the exact same ArtList envelope). Tolerant by inheritance: any
    decode/shape error returns ``[]``; url-less rows are skipped; timestamps
    are tz-aware UTC.
    """
    return _parse_gdelt(body, fallback_domain)


# Economic GKG theme → feed-slug pairs. These are GDELT GKG theme codes; the
# DOC API accepts them via the ``theme:`` filter and returns only articles GKG
# tagged with that theme. Slugs become the ``gdelt-gkg-<slug>`` feed suffix.
_THEME_QUERIES: tuple[tuple[str, str], ...] = (
    ("stockmarket", "theme:ECON_STOCKMARKET"),
    ("bankruptcy", "theme:ECON_BANKRUPTCY"),
    ("interestrate", "theme:ECON_INTEREST_RATE"),
    ("inflation", "theme:ECON_INFLATION"),
    ("financialsector", "theme:WB_2447_FINANCIAL_SECTOR_DEVELOPMENT"),
    ("earningsreport", "theme:ECON_EARNINGSREPORT"),
)


def _build_url(query: str) -> str:
    """Assemble a DOC 2.0 ArtList URL with the theme query URL-encoded.

    ``quote_plus`` encodes the ``:`` in ``theme:CODE`` so the assembled URL is
    valid. ``timespan=30min`` overlaps the poller's cadence (dedup handles the
    repeats); newest-first with a healthy per-query cap.
    """
    return (
        f"{_GDELT_DOC_API}"
        f"?query={quote_plus(query)}"
        "&mode=ArtList"
        "&format=json"
        "&timespan=30min"
        "&maxrecords=75"
        "&sort=DateDesc"
    )


# Register the parser at import time so `get_parser("gdelt_gkg")` resolves once
# this module is auto-discovered by catchem.news_sources.
register_parser("gdelt_gkg", _parse_gkg)


@register_feed_provider
def _gdelt_gkg_feeds() -> list[FeedSpec]:
    """Contribute the theme-targeted GDELT GKG DOC 2.0 queries."""
    return [
        FeedSpec(
            name=f"gdelt-gkg-{slug}",
            url=_build_url(query),
            fallback_domain="gdelt.org",
            parser="gdelt_gkg",
        )
        for slug, query in _THEME_QUERIES
    ]
