"""MACRO / ECON-DATA / THINK-TANK source pack.

Macro releases (Fed/OECD/BIS/IMF) and the research that interprets them drive
cross-asset moves long before single-name headlines do. This pack wires a
curated set of widely-known, stable, public RSS/Atom endpoints from central
banks, statistical/research institutions, think tanks, and the economics
blogosphere. Every entry uses the built-in "rss" parser (the FeedSpec
default) — no new body parser is needed, so this file just ADDS its own
FeedSpecs and nothing else.

Contract (kept deliberately small so packs can be authored in parallel):

    from ..news_poller import FeedSpec, register_feed_provider

    @register_feed_provider
    def _provider() -> list[FeedSpec]: ...

``assemble_feeds()`` merges this provider's output into DEFAULT_FEEDS and
de-dups by name, so dropping this file in is all that's required to enable it.
Names are ``macro-<slug>`` and chosen NOT to collide with the built-in feeds
(none of which start with ``macro-``).

Endpoint selection notes:
  * Central banks / multilaterals: NY Fed Liberty Street Economics, IMF blog,
    BIS research, OECD newsroom — all publish stable, long-lived feeds.
  * Research institutions: Brookings economics, PIIE, NBER new working papers,
    CEPR / VoxEU, Tax Foundation, Cato.
  * Economics blogosphere (econ commentary that routinely front-runs the
    market read on a release): Calculated Risk, Marginal Revolution,
    Conversable Economist, Econbrowser.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s
the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``macro-<slug>`` feed name.
# Fallback domain is the publisher's brand host, used only when an item lacks
# a resolvable link host (rare for these well-formed feeds).
_MACRO_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Central banks / multilateral institutions
    ("liberty-street", "https://libertystreeteconomics.newyorkfed.org/feed/", "newyorkfed.org"),
    ("imf-blog", "https://www.imf.org/en/Blogs/rss", "imf.org"),
    ("bis-research", "https://www.bis.org/doclist/all_rss.rss", "bis.org"),
    ("oecd-newsroom", "https://www.oecd.org/newsroom/index.xml", "oecd.org"),
    # ── St. Louis Fed (FRED) blog
    ("fred-blog", "https://fredblog.stlouisfed.org/feed/", "stlouisfed.org"),
    # ── Research institutions / think tanks
    ("brookings-economics", "https://www.brookings.edu/topic/economics/feed/", "brookings.edu"),
    ("piie", "https://www.piie.com/rss/update.xml", "piie.com"),
    ("nber-new-papers", "https://www.nber.org/rss/new.xml", "nber.org"),
    ("voxeu-cepr", "https://cepr.org/rss/vox.xml", "cepr.org"),
    ("tax-foundation", "https://taxfoundation.org/feed/", "taxfoundation.org"),
    ("cato", "https://www.cato.org/rss/recent-content", "cato.org"),
    # ── Economics blogosphere (interprets releases, often front-runs the read)
    ("calculated-risk", "https://www.calculatedriskblog.com/feeds/posts/default", "calculatedriskblog.com"),
    ("marginal-revolution", "https://marginalrevolution.com/feed", "marginalrevolution.com"),
    ("conversable-economist", "https://conversableeconomist.com/feed/", "conversableeconomist.com"),
    ("econbrowser", "https://econbrowser.com/feed", "econbrowser.com"),
)


@register_feed_provider
def macro_feeds() -> list[FeedSpec]:
    """Return the macro / econ-data / think-tank RSS feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"macro-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _MACRO_FEEDS
    ]
