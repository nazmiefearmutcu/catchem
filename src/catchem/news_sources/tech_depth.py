"""TECH / BUSINESS-DEPTH source pack — startup & megacap signal.

Tech and startup coverage frequently moves megacap and growth equities
(AAPL/MSFT/GOOGL/NVDA/META/AMZN/TSLA, the whole semis + SaaS complex) well
before — or alongside — the mainstream financial wires. A funding round, a
product launch, an antitrust headline, or a layoffs scoop on one of these
outlets is often the *first* place a thesis shows up. DEFAULT_FEEDS leans
mainstream-business + financial-press; this pack widens the aperture to the
dedicated tech/business press so those moves land in the Live Feed.

This is a pure-RSS pack: every FeedSpec uses ``parser="rss"`` (the built-in
RSS/Atom XML parser), so it registers **no** new parser — it only contributes
extra wires via ``@register_feed_provider``. Dropping this file is all it
takes to enable it; ``catchem.news_sources`` auto-discovers and imports it,
which fires the registration at import time, and ``assemble_feeds()`` merges
the result on top of DEFAULT_FEEDS (de-duped by name, DEFAULT_FEEDS wins).

Endpoint choices favour long-stable, no-auth, widely-known RSS URLs. Each
FeedSpec carries a realistic ``fallback_domain`` so an item that arrives
without a resolvable host is still attributed to the right publisher.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (name, url, fallback_domain) triples. Names are unique within this pack and
# namespaced loosely by publisher so they can't collide with DEFAULT_FEEDS
# (which uses bare publisher slugs like "bbc-tech"). All HTTPS, all RSS/Atom.
_TECH_DEPTH_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Core tech/startup press
    ("techcrunch", "https://techcrunch.com/feed/", "techcrunch.com"),
    ("techcrunch-startups", "https://techcrunch.com/category/startups/feed/", "techcrunch.com"),
    ("the-verge", "https://www.theverge.com/rss/index.xml", "theverge.com"),
    ("ars-technica", "https://feeds.arstechnica.com/arstechnica/index", "arstechnica.com"),
    ("wired-business", "https://www.wired.com/feed/category/business/latest/rss", "wired.com"),
    ("venturebeat", "https://venturebeat.com/feed/", "venturebeat.com"),
    ("engadget", "https://www.engadget.com/rss.xml", "engadget.com"),
    ("techmeme", "https://www.techmeme.com/feed.xml", "techmeme.com"),
    ("the-register", "https://www.theregister.com/headlines.atom", "theregister.com"),
    ("zdnet", "https://www.zdnet.com/news/rss.xml", "zdnet.com"),
    # ── Business-depth / explanatory
    ("axios", "https://api.axios.com/feed/", "axios.com"),
    ("semafor-business", "https://www.semafor.com/api/feeds/business.rss", "semafor.com"),
    ("quartz", "https://qz.com/rss", "qz.com"),
    ("business-insider", "https://www.businessinsider.com/rss", "businessinsider.com"),
    ("fast-company", "https://www.fastcompany.com/latest/rss", "fastcompany.com"),
    ("rest-of-world", "https://restofworld.org/feed/latest/", "restofworld.org"),
)


@register_feed_provider
def _tech_depth_feeds() -> list[FeedSpec]:
    """Contribute the tech/business-depth RSS feeds (parser defaults to rss)."""
    return [
        FeedSpec(name=name, url=url, fallback_domain=domain)
        for name, url, domain in _TECH_DEPTH_FEEDS
    ]
