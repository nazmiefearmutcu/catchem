"""HEALTHCARE / BIOTECH / PHARMA specialist source pack — the dedicated trade
press that leads generalist business coverage for drug-development moves.

FDA approval/rejection decisions, pivotal trial readouts, and pharma M&A are
some of the largest single-name movers in the market, yet a broadcast crawl or
a generalist business wire surfaces them late and without the domain context
(endpoint hit/missed, CRL vs. approval, label scope). DEFAULT_FEEDS has only
thin generalist coverage here; this pack reaches into the primary regulator and
the specialist desks that break (and contextualize) these moves:

  * Primary regulator — the U.S. FDA press-release RSS, the source the trade
    press itself reacts to for approvals, CRLs, and safety actions.
  * Pharma / biotech trade press — FiercePharma, FierceBiotech, STAT News,
    Endpoints News, BioPharma Dive, BioSpace, Healthcare Dive.
  * Clinical / device / sector desks — MedCity News, Clinical Trials Arena.

Every endpoint is plain RSS/Atom, so each FeedSpec uses the built-in
``parser="rss"`` (the FeedSpec default) and rides the exact same
fetch → dedup → ingest pipeline as the curated defaults. This file therefore
only ADDS its own FeedSpecs — it never edits DEFAULT_FEEDS or any other pack,
which keeps parallel authorship collision-free.

Naming + collision rules (mirrors the other packs):
  * Names are ``hc-<slug>`` (healthcare) and disjoint from DEFAULT_FEEDS and
    every other pack. ``assemble_feeds()`` de-dups by name, so a clash would
    silently drop this pack's entry — the test suite asserts disjointness to
    catch that.
  * ``fallback_domain`` is each publisher's bare brand host (no scheme/path),
    used only when an item arrives without a resolvable link host.

If you add new sources, fetch them once with a real UA before checking in — the
poller's circuit breaker will quietly cool down any endpoint that 403s or 404s
the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``hc-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
_HEALTHCARE_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Primary regulator (what the trade press reacts to) ───────────────────
    # U.S. FDA press announcements — documented public RSS from fda.gov.
    (
        "fda-press",
        "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
        "fda.gov",
    ),
    # ── Pharma / biotech trade press ─────────────────────────────────────────
    # FiercePharma — pharma industry desk (Industry Dive WordPress feed).
    ("fiercepharma", "https://www.fiercepharma.com/rss/xml", "fiercepharma.com"),
    # FierceBiotech — biotech / clinical-development desk.
    ("fiercebiotech", "https://www.fiercebiotech.com/rss/xml", "fiercebiotech.com"),
    # STAT News — health/biotech/pharma newsroom site-wide RSS (WordPress feed).
    ("stat-news", "https://www.statnews.com/feed/", "statnews.com"),
    # Endpoints News — biopharma R&D / deals desk site-wide RSS (WordPress feed).
    ("endpoints", "https://endpts.com/feed/", "endpts.com"),
    # BioPharma Dive — Industry Dive biopharma desk RSS.
    ("biopharma-dive", "https://www.biopharmadive.com/feeds/news/", "biopharmadive.com"),
    # BioSpace — biotech industry news & careers RSS.
    ("biospace", "https://www.biospace.com/rss/news", "biospace.com"),
    # Healthcare Dive — Industry Dive healthcare-sector desk RSS.
    ("healthcare-dive", "https://www.healthcaredive.com/feeds/news/", "healthcaredive.com"),
    # ── Clinical / device / sector desks ─────────────────────────────────────
    # MedCity News — health-tech / med-device / pharma business RSS (WordPress).
    ("medcity-news", "https://medcitynews.com/feed/", "medcitynews.com"),
    # Clinical Trials Arena — clinical-trial industry desk (GlobalData) RSS.
    ("clinical-trials-arena", "https://www.clinicaltrialsarena.com/feed/", "clinicaltrialsarena.com"),
)


@register_feed_provider
def healthcare_feeds() -> list[FeedSpec]:
    """Return the HEALTHCARE / BIOTECH / PHARMA specialist RSS/Atom feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"hc-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _HEALTHCARE_FEEDS
    ]
