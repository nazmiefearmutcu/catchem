"""EARNINGS / INVESTOR-RELATIONS / PRESS-RELEASE source pack — corporate
primary-source announcements, the earliest and highest-signal market movers.

`DEFAULT_FEEDS` already carries PR Newswire (the `prnewswire-all` and
`prnewswire-financial` wires) plus general business desks. What it is missing
is the rest of the corporate press-release / investor-relations layer: the
OTHER major wire services (GlobeNewswire, Business Wire, ACCESSWIRE, EIN
Presswire), the exchanges' own corporate-action / press desks (Nasdaq, NYSE /
Intercontinental Exchange), the earnings-calendar and transcript firehoses
(Seeking Alpha transcripts, StockTitan, Motley Fool earnings, Zacks), and the
"calendar of events" wire. These are the primary sources a company controls
itself, so a beat/miss or M&A headline shows up here BEFORE a desk re-reports
it.

Design (mirrors the other packs):
  * Pure RSS — every feed uses the built-in ``parser="rss"`` (RSS/Atom XML),
    so there is NO new parser here. The pack only calls
    `register_feed_provider`.
  * Publisher-documented endpoints — each URL is the publisher's own
    well-known, no-auth RSS path. Where a wire exposes several feeds we prefer
    the broad "all releases" or finance/earnings section.
  * Realistic `fallback_domain` per source — each publisher's bare brand host,
    so an item arriving without a usable link still attributes correctly.
  * Additive only — this module never edits the shared DEFAULT_FEEDS tuple.
    `assemble_feeds()` merges it in and de-dupes by name (DEFAULT_FEEDS wins
    ties), which keeps parallel source-pack authorship collision-free.

Names are prefixed ``ir-`` (investor relations) to stay clear of any
DEFAULT_FEEDS name (incl. the existing PR Newswire entries) and every other
pack. `assemble_feeds()` de-dups by name, so a clash would silently drop this
pack's entry — the test suite asserts disjointness to catch that.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s or
404s the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``ir-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
# PR Newswire is intentionally absent — DEFAULT_FEEDS already owns it.
_EARNINGS_IR_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Major press-release wire services (NOT prnewswire — already default) ─
    # GlobeNewswire public "News about Public Companies" newsroom RSS.
    (
        "globenewswire",
        "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
        "globenewswire.com",
    ),
    # GlobeNewswire "Calendar of Events" public RSS.
    (
        "globenewswire-events",
        "https://www.globenewswire.com/RssFeed/subjectcode/14-Calendar%20of%20Events/feedTitle/GlobeNewswire%20-%20Calendar%20of%20Events",
        "globenewswire.com",
    ),
    # Business Wire public "all news" newsroom RSS.
    ("businesswire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==", "businesswire.com"),
    # ACCESSWIRE (issuer-direct newswire) public latest-releases RSS.
    ("accesswire", "https://www.accesswire.com/users/rss/latest", "accesswire.com"),
    # EIN Presswire — Business & Economy topical RSS.
    ("einpresswire-business", "https://www.einpresswire.com/rss/business-economy", "einpresswire.com"),
    # ── Exchanges' own corporate press desks ────────────────────────────────
    # Nasdaq press-release RSS (the exchange's corporate newsroom).
    ("nasdaq-press", "https://www.nasdaq.com/feed/rssoutbound?category=Press+Releases", "nasdaq.com"),
    # Intercontinental Exchange (parent of NYSE) news-releases RSS feed.
    ("ice-nyse-press", "https://ir.theice.com/rss/news-releases.xml", "ir.theice.com"),
    # ── Earnings transcripts / calendars / coverage firehoses ───────────────
    # Seeking Alpha "Transcripts" section RSS (earnings-call transcripts).
    ("seekingalpha-transcripts", "https://seekingalpha.com/feed/transcripts.xml", "seekingalpha.com"),
    # StockTitan latest market-news / press-release RSS.
    ("stocktitan", "https://www.stocktitan.net/rss/all.xml", "stocktitan.net"),
    # Quartr blog RSS (earnings-call / IR commentary).
    ("quartr", "https://quartr.com/rss.xml", "quartr.com"),
    # The Motley Fool earnings-call-transcript section RSS.
    ("motleyfool-earnings", "https://www.fool.com/feeds/index.aspx?categoryid=1081", "fool.com"),
    # Zacks commentary RSS (earnings estimates / revisions coverage).
    ("zacks", "https://www.zacks.com/rss/rss_news.php", "zacks.com"),
)


@register_feed_provider
def earnings_ir_feeds() -> list[FeedSpec]:
    """Return the EARNINGS / INVESTOR-RELATIONS / PRESS-RELEASE RSS feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"ir-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _EARNINGS_IR_FEEDS
    ]
