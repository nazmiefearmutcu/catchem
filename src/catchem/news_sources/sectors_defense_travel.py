"""DEFENSE / AEROSPACE / AIRLINES / TRAVEL sector source pack — two distinct
sector engines under one roof, each with its own dedicated trade press.

Defense & aerospace moves on geopolitics: procurement contracts, budget
authorizations, conflict escalation, and program milestones break in the
specialist defense press long before a generalist crawl picks them up.
Airlines & travel move on a different driver entirely — travel demand,
capacity, fuel, fares, and loyalty economics — covered by the aviation /
travel trade desks. DEFAULT_FEEDS has no dedicated coverage of either; this
pack reaches into the primary trade publications themselves:

  * Defense / aerospace —
      Defense News, Breaking Defense, Aviation Week, FlightGlobal,
      The War Zone (TWZ), SpaceNews.
  * Airlines / travel —
      Skift, Simple Flying, Airline Weekly, Travel Weekly, AeroTime.

(The Points Guy is a consumer-rewards site, not a sector desk, so it is
deliberately omitted in favor of Simple Flying / AeroTime for airline news.)

Every endpoint is plain RSS/Atom, so each FeedSpec uses the built-in
``parser="rss"`` (the FeedSpec default) and rides the exact same
fetch → dedup → ingest pipeline as the curated defaults. This file therefore
only ADDS its own FeedSpecs — it never edits DEFAULT_FEEDS or any other pack,
which keeps parallel authorship collision-free.

Naming + collision rules (mirrors the other packs):
  * Names are ``dt-<slug>`` (defense/travel) and disjoint from DEFAULT_FEEDS
    and every other pack. ``assemble_feeds()`` de-dups by name, so a clash
    would silently drop this pack's entry — the test suite asserts
    disjointness to catch that.
  * ``fallback_domain`` is each publisher's bare brand host (no scheme/path),
    used only when an item arrives without a resolvable link host.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s or
404s the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``dt-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
_DEFENSE_TRAVEL_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Defense / aerospace trade press ─────────────────────────────────────
    # Defense News — leading defense procurement / policy desk (WordPress feed).
    ("defense-news", "https://www.defensenews.com/arc/outboundfeeds/rss/", "defensenews.com"),
    # Breaking Defense — defense industry & policy news (WordPress feed).
    ("breaking-defense", "https://breakingdefense.com/feed/", "breakingdefense.com"),
    # Aviation Week Network — aerospace/defense trade press RSS.
    ("aviation-week", "https://aviationweek.com/rss.xml", "aviationweek.com"),
    # FlightGlobal — commercial & defense aviation industry desk RSS.
    ("flightglobal", "https://www.flightglobal.com/rss", "flightglobal.com"),
    # The War Zone (TWZ) on The Drive — military hardware / conflict desk
    # (WordPress feed scoped to the category).
    ("twz", "https://www.twz.com/feed", "twz.com"),
    # SpaceNews — space industry, launch & national-security space desk
    # (WordPress feed).
    ("spacenews", "https://spacenews.com/feed/", "spacenews.com"),
    # ── Airlines / travel trade press ───────────────────────────────────────
    # Skift — travel industry intelligence desk (WordPress feed).
    ("skift", "https://skift.com/feed/", "skift.com"),
    # Simple Flying — airline / aviation news desk (WordPress feed).
    ("simple-flying", "https://simpleflying.com/feed/", "simpleflying.com"),
    # Airline Weekly (a Skift property) — airline business / financial desk.
    ("airline-weekly", "https://airlineweekly.com/feed/", "airlineweekly.com"),
    # Travel Weekly — travel trade press (industry/business) RSS.
    ("travel-weekly", "https://www.travelweekly.com/rss", "travelweekly.com"),
    # AeroTime — aviation industry news desk (WordPress feed).
    ("aerotime", "https://www.aerotime.aero/feed", "aerotime.aero"),
)


@register_feed_provider
def defense_travel_feeds() -> list[FeedSpec]:
    """Return the DEFENSE / AEROSPACE / AIRLINES / TRAVEL sector RSS/Atom feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"dt-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _DEFENSE_TRAVEL_FEEDS
    ]
