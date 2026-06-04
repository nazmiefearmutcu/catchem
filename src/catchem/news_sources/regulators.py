"""Central banks + financial regulators source pack — top-signal feeds.

Official central-bank and regulator releases are the single highest-signal
market movers: rate decisions, enforcement actions, jobs/CPI/GDP prints, and
filings routinely move whole asset classes within seconds of publication.
This pack contributes the publishers' documented public RSS/Atom endpoints
that are NOT already in ``DEFAULT_FEEDS`` (which already carries the Fed
press feed, three SEC EDGAR Atom feeds, and the ECB press feed — those names
are intentionally avoided here so ``assemble_feeds()`` keeps them).

No new parser: every endpoint below is plain RSS/Atom, so each FeedSpec uses
the built-in ``parser="rss"`` and rides the exact same fetch → dedup → ingest
pipeline as the curated defaults. ``fallback_domain`` is set to each
publisher's canonical host (``federalreserve.gov``-style) so an item that
arrives without a usable link still attributes correctly in the Live Feed.

Coverage (15 feeds):
  * Central banks   — Bank of England, Bank of Japan, Bank of Canada,
                      Reserve Bank of Australia.
  * US Treasury / markets regulators — US Treasury press, CFTC press,
                      SEC litigation, SEC press, FINRA, FDIC, OCC.
  * US statistics   — BLS (jobs/CPI), BEA (GDP).
  * International   — IMF news, BIS, World Bank news.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (name, url, fallback_domain) triples. Names are unique within this pack and
# disjoint from DEFAULT_FEEDS (fed-press-all / sec-edgar-current / sec-8k /
# sec-10q / ecb-press). All URLs are https and point at the publisher's
# documented RSS/Atom endpoint.
_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Central banks ──────────────────────────────────────────────────────
    ("boe-news", "https://www.bankofengland.co.uk/rss/news", "bankofengland.co.uk"),
    ("boj-announcements", "https://www.boj.or.jp/en/rss/whatsnew.xml", "boj.or.jp"),
    ("boc-press", "https://www.bankofcanada.ca/content_type/press-releases/feed/", "bankofcanada.ca"),
    ("rba-media-releases", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml", "rba.gov.au"),
    # ── US Treasury + markets regulators ───────────────────────────────────
    ("us-treasury-press", "https://home.treasury.gov/system/files/126/press_releases.xml", "home.treasury.gov"),
    ("cftc-press", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", "cftc.gov"),
    ("sec-litigation", "https://www.sec.gov/rss/litigation/litreleases.xml", "sec.gov"),
    ("sec-press", "https://www.sec.gov/news/pressreleases.rss", "sec.gov"),
    ("finra-news", "https://www.finra.org/about/news-center/news-releases/feed", "finra.org"),
    ("fdic-press", "https://www.fdic.gov/news/press-releases/rss.xml", "fdic.gov"),
    ("occ-news", "https://www.occ.gov/rss/occ_media_news.xml", "occ.gov"),
    # ── US statistics releases ─────────────────────────────────────────────
    ("bls-news", "https://www.bls.gov/feed/bls_latest.rss", "bls.gov"),
    ("bea-news", "https://apps.bea.gov/rss/rss.xml", "bea.gov"),
    # ── International institutions ─────────────────────────────────────────
    ("imf-news", "https://www.imf.org/en/News/RSS?Language=ENG", "imf.org"),
    ("bis-news", "https://www.bis.org/list/press_rss.xml", "bis.org"),
    ("worldbank-news", "https://www.worldbank.org/en/news/all.rss", "worldbank.org"),
)


@register_feed_provider
def _regulator_feeds() -> list[FeedSpec]:
    """Contribute the central-bank + regulator RSS/Atom feeds (parser='rss')."""
    return [FeedSpec(name=name, url=url, fallback_domain=domain) for name, url, domain in _FEEDS]
