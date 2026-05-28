"""GOVERNMENT / ECONOMIC-DATA-RELEASE source pack — scheduled prints + gazettes.

Where the ``regulators`` pack carries the *central-bank / financial-supervisor*
firehose (Fed/SEC/Treasury/CFTC/BLS/BEA/IMF/BIS/World Bank/BoE/BoJ/…), this
pack targets a DIFFERENT, complementary surface: the **scheduled statistical
data releases and official government gazettes** that move markets on a
calendar the desk can see coming — national statistics offices, the US
Federal Register (the rulebook of record), Census economic indicators, and
agriculture/budget bodies whose reports routinely reprice commodities, rates,
and equities the moment they hit.

Why this is distinct from the existing packs (no duplication):
  * ``regulators`` already owns BLS, BEA, US Treasury, SEC, CFTC, FDIC, OCC,
    FINRA, IMF, BIS, World Bank, and the BoE/BoJ/BoC/RBA central banks — none
    of those publishers are repeated here.
  * ``macro`` already owns the OECD *newsroom* feed (``macro-oecd-newsroom``)
    and the research/think-tank blogosphere — so this pack takes the OECD
    *newsletters/data* surface under a distinct slug instead, and steers clear
    of every ``macro-`` name.
  * ``commodities_energy`` already owns the US EIA feeds (``ce-eia-*``) — the
    EIA is therefore intentionally SKIPPED here to avoid a duplicate.

Everything below is plain RSS/Atom, so each FeedSpec uses the built-in
``parser="rss"`` (the FeedSpec default) and rides the exact same
fetch → dedup → ingest pipeline as the curated defaults. This file only ADDS
its own FeedSpecs — it never edits ``DEFAULT_FEEDS`` or any other pack, which
keeps parallel source-pack authorship collision-free.

Naming + collision rules (mirrors the other packs):
  * Names are ``gov-<slug>`` and disjoint from ``DEFAULT_FEEDS`` and every
    other pack. ``assemble_feeds()`` de-dups by name, so a clash would
    silently drop this pack's entry — the test suite asserts disjointness to
    catch that.
  * ``fallback_domain`` is each publisher's bare brand host (no scheme/path),
    used only when an item arrives without a resolvable link host.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s or
404s the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``gov-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
_GOVDATA_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── US Federal Register — the daily journal of the US government. The
    # "economy and finance" topic/section RSS surfaces proposed + final rules,
    # notices, and presidential documents that move regulated industries. (The
    # Federal Register is NOT in the regulators pack, which carries the agency
    # press feeds, not the gazette of record.)
    (
        "us-federal-register-economy",
        "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Btopics%5D%5B%5D=economy-and-finance",
        "federalregister.gov",
    ),
    # ── Eurostat — the EU statistical office. "What's new" news RSS carries the
    # release calendar prints (HICP/flash inflation, GDP, unemployment, trade).
    ("eurostat-news", "https://ec.europa.eu/eurostat/web/main/news/whats-new?p_p_lifecycle=2&p_p_resource_id=rss", "ec.europa.eu"),
    # ── Eurostat euro-indicators — the dedicated PEEI / euro-indicators release
    # stream (the scheduled "Euro area" headline prints), distinct from the
    # general news feed above.
    ("eurostat-euro-indicators", "https://ec.europa.eu/eurostat/web/euro-indicators/w/rss", "ec.europa.eu"),
    # ── UK Office for National Statistics — all ONS statistical releases
    # (CPI/CPIH, GDP, labour market). ONS is the UK's national stats office and
    # is NOT in the regulators pack (which carries the Bank of England).
    ("uk-ons-releases", "https://www.ons.gov.uk/releasecalendar?rss", "ons.gov.uk"),
    # ── US Census Bureau — economic-indicators newsroom RSS (retail sales,
    # durable goods, new home sales, trade balance, business inventories). The
    # Census is distinct from BLS/BEA already in the regulators pack.
    ("us-census-economic-indicators", "https://www.census.gov/economic-indicators/indicator.xml", "census.gov"),
    # ── Statistics Canada — "The Daily" is StatCan's official release channel
    # (CPI, GDP, Labour Force Survey). Distinct from the Bank of Canada press
    # feed in the regulators pack.
    ("statcan-the-daily", "https://www150.statcan.gc.ca/n1/dai-quo/ssi/homepage-eng.rss", "statcan.gc.ca"),
    # ── OECD — newsletters / data surface. The OECD *newsroom* feed lives in
    # the macro pack (``macro-oecd-newsroom``); this is the distinct
    # publications/newsletter stream, kept under a clearly different slug so the
    # two never collide.
    ("oecd-newsletters", "https://www.oecd.org/en/newsletters/oecd-newsletters.xml", "oecd.org"),
    # ── US Department of Labor — agency news releases (jobs programs, OSHA,
    # wage/hour rulemaking, ETA grants). This is the DOL *agency* news feed,
    # distinct from the BLS statistical feed (``bls-news``) in the regulators
    # pack — DOL ≠ its BLS sub-agency.
    ("us-dol-news", "https://www.dol.gov/rss/releases.xml", "dol.gov"),
    # ── US Department of Agriculture — USDA newsroom RSS. WASDE / crop
    # production / ag-prices reports out of USDA reprice grains, softs, and
    # livestock; not covered by any existing pack (commodities_energy carries
    # energy/metals trade press + EIA, not USDA).
    ("usda-news", "https://www.usda.gov/rss/latest-releases.xml", "usda.gov"),
    # ── Congressional Budget Office — CBO publications RSS (budget & economic
    # outlook, cost estimates, long-term projections). Moves Treasury issuance
    # expectations and the rates complex; not in any existing pack.
    ("us-cbo-publications", "https://www.cbo.gov/publications/all/rss.xml", "cbo.gov"),
    # ── US Bureau of Transportation Statistics — official transportation
    # indicators (freight index, airline traffic, the Transportation Services
    # Index) that read through to industrial activity. A scheduled
    # government-data release surface not held by any other pack.
    ("us-bts-news", "https://www.bts.gov/rss/press-releases", "bts.gov"),
)


@register_feed_provider
def govdata_feeds() -> list[FeedSpec]:
    """Return the GOVERNMENT / ECONOMIC-DATA-RELEASE RSS/Atom feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"gov-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _GOVDATA_FEEDS
    ]
