"""REAL-ESTATE / REIT / INDUSTRIALS / AUTOS sector source pack — the dedicated
trade press that leads generalist business coverage for these sectors.

Housing, commercial real estate, mortgages, autos/EVs, manufacturing,
supply-chain, and construction each have specialist desks that break (and
contextualize) sector moves long before they reach a broadcast crawl or a
generalist wire. DEFAULT_FEEDS and the broad Google-News queries cover the
obvious aggregate; this pack reaches into the primary trade publications:

  * Real estate / REIT / housing / mortgages — HousingWire, The Real Deal,
    Inman, Bisnow, GlobeSt, Mortgage News Daily.
  * Autos & EVs — Automotive News, Electrek, InsideEVs.
  * Industrials / manufacturing — IndustryWeek, Supply Chain Dive,
    Manufacturing.net.
  * Construction — Construction Dive.

(Calculated Risk is intentionally skipped here — it is a macro/housing blog
already in scope elsewhere; this pack avoids re-listing it.)

Every endpoint is plain RSS/Atom, so each FeedSpec uses the built-in
``parser="rss"`` (the FeedSpec default) and rides the exact same
fetch → dedup → ingest pipeline as the curated defaults. This file therefore
only ADDS its own FeedSpecs — it never edits DEFAULT_FEEDS or any other pack,
which keeps parallel authorship collision-free.

Naming + collision rules (mirrors the other packs):
  * Names are ``sri-<slug>`` (sectors: real-estate / industrials) and disjoint
    from DEFAULT_FEEDS and every other pack. NOTE: the brief suggested a
    ``sec-`` prefix, but ``sec-`` already collides — DEFAULT_FEEDS ships
    ``sec-edgar-current`` / ``sec-8k`` / ``sec-10q`` and the regulators pack
    emits ``sec-litigation`` / ``sec-press`` — and ``assemble_feeds()``
    de-dups by name (first occurrence wins), which would silently DROP a
    same-named entry. ``sri-`` is disjoint from every existing prefix
    (ce/gdelt/gnews/hc/hn/macro/reddit/spec/x/yt/ir/sec/reg/...). The test
    suite asserts disjointness to catch any future drift.
  * ``fallback_domain`` is each publisher's bare brand host (no scheme/path),
    used only when an item arrives without a resolvable link host.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s or
404s the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``sri-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
_SECTOR_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Real estate / REIT / housing / mortgages ────────────────────────────
    # HousingWire — housing finance & real estate trade press (WordPress feed).
    ("housingwire", "https://www.housingwire.com/feed/", "housingwire.com"),
    # The Real Deal — commercial & residential real estate news (WordPress).
    ("therealdeal", "https://therealdeal.com/feed/", "therealdeal.com"),
    # Inman — residential real estate / brokerage industry news (WordPress).
    ("inman", "https://www.inman.com/feed/", "inman.com"),
    # Bisnow — commercial real estate news RSS.
    ("bisnow", "https://www.bisnow.com/rss", "bisnow.com"),
    # GlobeSt — commercial real estate (ALM) news RSS.
    ("globest", "https://www.globest.com/feed/", "globest.com"),
    # Mortgage News Daily — mortgage rates & lending trade desk RSS.
    ("mortgage-news-daily", "https://www.mortgagenewsdaily.com/rss/full", "mortgagenewsdaily.com"),
    # ── Autos & EVs ─────────────────────────────────────────────────────────
    # Automotive News — the auto-industry trade paper RSS.
    ("automotive-news", "https://www.autonews.com/arc/outboundfeeds/rss/", "autonews.com"),
    # Electrek — EV / clean-transport news (WordPress feed).
    ("electrek", "https://electrek.co/feed/", "electrek.co"),
    # InsideEVs — electric-vehicle news (WordPress feed).
    ("insideevs", "https://insideevs.com/rss/articles/all/", "insideevs.com"),
    # ── Industrials / manufacturing ─────────────────────────────────────────
    # IndustryWeek — manufacturing & industrial operations trade press RSS.
    ("industryweek", "https://www.industryweek.com/rss.xml", "industryweek.com"),
    # Supply Chain Dive — logistics & supply-chain news RSS (Industry Dive).
    ("supplychain-dive", "https://www.supplychaindive.com/feeds/news/", "supplychaindive.com"),
    # Manufacturing.net — manufacturing industry news RSS.
    ("manufacturing-net", "https://www.manufacturing.net/rss/all", "manufacturing.net"),
    # ── Construction ────────────────────────────────────────────────────────
    # Construction Dive — construction industry news RSS (Industry Dive).
    ("construction-dive", "https://www.constructiondive.com/feeds/news/", "constructiondive.com"),
)


@register_feed_provider
def sectors_realestate_industrials_feeds() -> list[FeedSpec]:
    """Return the REAL-ESTATE / REIT / INDUSTRIALS / AUTOS sector RSS/Atom feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"sri-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _SECTOR_FEEDS
    ]
