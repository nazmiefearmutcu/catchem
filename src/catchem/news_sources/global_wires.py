"""GLOBAL / REGIONAL wire source pack — broaden beyond US/UK.

`DEFAULT_FEEDS` in `news_poller` skews US/UK (BBC, Reuters, CNBC, WSJ, Fed,
SEC, …). Market-moving news, though, breaks first in Tokyo, Frankfurt,
Mumbai, Singapore, and Sydney — an Asian session reaction to a US close,
an ECB headline hours before New York opens, an India/EM macro print. This
pack contributes a curated set of widely-known, stable, no-auth RSS feeds
from publishers across Asia, Europe, and emerging markets so the Live Feed
catches those moves in their home time zone instead of waiting for a US
desk to re-report them.

Design:
  * Pure RSS — every feed uses the built-in ``parser="rss"`` (RSS/Atom XML),
    so there is NO new parser here. The pack only calls
    `register_feed_provider`.
  * Publisher-documented endpoints — each URL is the publisher's own
    well-known RSS path (DW/France24/Euronews/CNA/Nikkei/SCMP/ToI/ET/
    Moneycontrol/Livemint/Globe and Mail/ABC/Straits Times). Where a site
    exposes several feeds we prefer the business/markets section.
  * Realistic `fallback_domain` per source — the brand host, so items that
    arrive without a usable link still attribute correctly in the UI.
  * Additive only — this module never edits the shared DEFAULT_FEEDS tuple.
    `assemble_feeds()` merges it in and de-dupes by name (DEFAULT_FEEDS
    wins ties), which keeps parallel source-pack authorship collision-free.

Names are prefixed ``gw-`` (global wires) to stay clear of any DEFAULT_FEEDS
name and any other pack.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# Curated GLOBAL/REGIONAL business & markets RSS feeds. Each tuple is
# (name, url, fallback_domain); the parser is the built-in "rss" default.
# Every URL is a publisher-documented, no-auth RSS/Atom endpoint.
_GLOBAL_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Asia
    ("gw-nikkei-asia", "https://asia.nikkei.com/rss/feed/nar", "asia.nikkei.com"),
    ("gw-scmp-business", "https://www.scmp.com/rss/92/feed", "scmp.com"),
    (
        "gw-channelnewsasia-business",
        "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936",
        "channelnewsasia.com",
    ),
    ("gw-straitstimes-business", "https://www.straitstimes.com/news/business/rss.xml", "straitstimes.com"),
    ("gw-timesofindia-business", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms", "timesofindia.indiatimes.com"),
    (
        "gw-economictimes-markets",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "economictimes.indiatimes.com",
    ),
    ("gw-moneycontrol-business", "https://www.moneycontrol.com/rss/business.xml", "moneycontrol.com"),
    ("gw-livemint-markets", "https://www.livemint.com/rss/markets", "livemint.com"),
    ("gw-abc-au-business", "https://www.abc.net.au/news/feed/51892/rss.xml", "abc.net.au"),
    # ── Europe
    ("gw-dw-business", "https://rss.dw.com/rdf/rss-en-bus", "dw.com"),
    ("gw-dw-top", "https://rss.dw.com/rdf/rss-en-all", "dw.com"),
    ("gw-france24-business", "https://www.france24.com/en/business/rss", "france24.com"),
    ("gw-euronews-business", "https://www.euronews.com/business/rss", "euronews.com"),
    # ── North America (non-US)
    ("gw-globeandmail-business", "https://www.theglobeandmail.com/business/?service=rss", "theglobeandmail.com"),
)


@register_feed_provider
def _global_wire_feeds() -> list[FeedSpec]:
    """Contribute the GLOBAL/REGIONAL business & markets RSS feeds.

    Every spec uses the default RSS parser; only the URL/domain differ.
    """
    return [
        FeedSpec(name=name, url=url, fallback_domain=fallback_domain)
        for name, url, fallback_domain in _GLOBAL_FEEDS
    ]
