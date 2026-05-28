"""COMMODITIES / ENERGY / RATES specialist source pack — the dedicated trade
press that leads generalist business coverage for these asset classes.

Oil, gas, metals, mining, freight/shipping, and the rate-sensitive macro
calendar all have their own specialist desks that break (and contextualize)
moves long before they surface on a broadcast crawl or a generalist wire.
DEFAULT_FEEDS has one generic energy Google-News query (``gnews-energy``);
this pack reaches into the primary trade publications themselves:

  * Oil & gas trade press — OilPrice.com, Rigzone, World Oil, Hart Energy.
  * Commodity price benchmarks — S&P Global Commodity Insights (formerly
    Platts), Kitco (metals).
  * Primary government energy data — the U.S. EIA "Today in Energy" feed,
    which is the source the trade press itself reacts to.
  * Mining & metals — Mining.com.
  * Freight / shipping / logistics rates (a real-time read on physical
    commodity flow) — gCaptain, FreightWaves.
  * Rates / macro context for the rate-sensitive commodity complex —
    Investing.com commodities + economic-calendar feeds.

Every endpoint is plain RSS/Atom, so each FeedSpec uses the built-in
``parser="rss"`` (the FeedSpec default) and rides the exact same
fetch → dedup → ingest pipeline as the curated defaults. This file therefore
only ADDS its own FeedSpecs — it never edits DEFAULT_FEEDS or any other pack,
which keeps parallel authorship collision-free.

Naming + collision rules (mirrors the other packs):
  * Names are ``ce-<slug>`` (commodities/energy) and disjoint from
    DEFAULT_FEEDS and every other pack. ``assemble_feeds()`` de-dups by name,
    so a clash would silently drop this pack's entry — the test suite asserts
    disjointness to catch that.
  * ``fallback_domain`` is each publisher's bare brand host (no scheme/path),
    used only when an item arrives without a resolvable link host.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s or
404s the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``ce-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
_COMMODITIES_ENERGY_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Oil & gas trade press ───────────────────────────────────────────────
    # OilPrice.com publishes a documented site-wide RSS feed of latest news.
    ("oilprice", "https://oilprice.com/rss/main", "oilprice.com"),
    # Rigzone news RSS (upstream oil & gas industry desk).
    ("rigzone", "https://www.rigzone.com/news/rss/rigzone_latest.aspx", "rigzone.com"),
    # World Oil — drilling/production trade press, WordPress feed endpoint.
    ("world-oil", "https://www.worldoil.com/rss?feed=news", "worldoil.com"),
    # Hart Energy (Oil & Gas Investor / E&P) latest-news RSS.
    ("hart-energy", "https://www.hartenergy.com/rss.xml", "hartenergy.com"),
    # ── Commodity price benchmarks ──────────────────────────────────────────
    # S&P Global Commodity Insights (formerly Platts) latest-news RSS.
    (
        "spglobal-commodities",
        "https://www.spglobal.com/commodityinsights/en/rss-feed/latest-news",
        "spglobal.com",
    ),
    # Kitco metals commentary/news RSS (gold/silver/PM desk).
    ("kitco-metals", "https://www.kitco.com/rss/KitcoNews.xml", "kitco.com"),
    # ── Primary government energy data (what the trade press reacts to) ──────
    # U.S. EIA "Today in Energy" — documented public RSS from the Energy
    # Information Administration.
    ("eia-today-in-energy", "https://www.eia.gov/rss/todayinenergy.xml", "eia.gov"),
    # U.S. EIA press releases RSS.
    ("eia-press", "https://www.eia.gov/rss/press_rss.xml", "eia.gov"),
    # ── Mining & metals ─────────────────────────────────────────────────────
    # Mining.com site-wide RSS (WordPress feed).
    ("mining-com", "https://www.mining.com/feed/", "mining.com"),
    # ── Freight / shipping / logistics rates (physical-flow read-through) ────
    # gCaptain maritime / shipping news RSS (WordPress feed).
    ("gcaptain", "https://gcaptain.com/feed/", "gcaptain.com"),
    # FreightWaves logistics & freight-rate news RSS (WordPress feed).
    ("freightwaves", "https://www.freightwaves.com/news/feed", "freightwaves.com"),
    # ── Rates / macro context for the commodity complex ─────────────────────
    # Investing.com commodities news RSS.
    ("investing-commodities", "https://www.investing.com/rss/news_11.rss", "investing.com"),
    # Investing.com economic-calendar / latest-economy RSS (rate-sensitive
    # macro prints that drive the commodity + rates complex).
    ("investing-economy", "https://www.investing.com/rss/news_95.rss", "investing.com"),
)


@register_feed_provider
def commodities_energy_feeds() -> list[FeedSpec]:
    """Return the COMMODITIES / ENERGY / RATES specialist RSS/Atom feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"ce-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _COMMODITIES_ENERGY_FEEDS
    ]
