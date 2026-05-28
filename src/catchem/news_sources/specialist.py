"""SPECIALIST / niche high-signal source pack — communities + research that
lead mainstream coverage.

The mainstream business desks (DEFAULT_FEEDS) and the macro / regulator packs
already cover the obvious wires. This pack reaches one layer deeper, into the
narrow communities, primary-source government dockets, and contrarian
commentary that frequently surface a story DAYS before it shows up on a
broadcast crawl:

  * Quant / econ Q&A communities (Quantitative Finance + Economics Stack
    Exchange) — where practitioners debate market microstructure, model
    breakage, and policy mechanics in real time.
  * arXiv q-fin — the working-paper firehose for quantitative finance.
  * SSRN's Financial Economics Network — pre-publication finance research.
  * Primary US rulemaking sources — the Federal Register's Treasury /
    economy document feed and Regulations.gov's newest dockets, which carry
    the actual rule text long before the trade-press summary.
  * Heterodox / contrarian macro blogs that routinely front-run the
    consensus read (Naked Capitalism, Wolf Street, Pragmatic Capitalism,
    The Big Picture, Project Syndicate economics, Bloomberg Opinion).

Every endpoint is plain RSS/Atom, so each FeedSpec uses the built-in
``parser="rss"`` (the FeedSpec default) and rides the exact same
fetch → dedup → ingest pipeline as the curated defaults. This file therefore
only ADDS its own FeedSpecs — it never edits DEFAULT_FEEDS or any other pack,
which keeps parallel authorship collision-free.

Naming + collision rules (mirrors the other packs):
  * Names are ``spec-<slug>`` and disjoint from DEFAULT_FEEDS and every other
    pack. ``assemble_feeds()`` de-dups by name, so a clash would silently drop
    this pack's entry — the test suite asserts disjointness to catch that.
  * ``fallback_domain`` is each publisher's bare brand host (no scheme/path),
    used only when an item arrives without a resolvable link host.

If you add new sources, fetch them once with a real UA before checking in —
the poller's circuit breaker will quietly cool down any endpoint that 403s or
404s the common UA, but it's better not to ship a dead feed.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (slug, url, fallback_domain). Slugs become the ``spec-<slug>`` feed name.
# All URLs are https and point at the publisher's documented RSS/Atom endpoint.
_SPECIALIST_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Practitioner Q&A communities (lead-indicator chatter) ───────────────
    # Stack Exchange exposes a documented Atom feed at /feeds on every site.
    ("quant-se", "https://quant.stackexchange.com/feeds", "quant.stackexchange.com"),
    ("economics-se", "https://economics.stackexchange.com/feeds", "economics.stackexchange.com"),
    ("money-se", "https://money.stackexchange.com/feeds", "money.stackexchange.com"),
    # ── Primary research firehoses ──────────────────────────────────────────
    # arXiv quantitative finance new-submissions RSS (documented export host).
    ("arxiv-qfin", "https://rss.arxiv.org/rss/q-fin", "arxiv.org"),
    # arXiv economics (general economics) — companion to q-fin.
    ("arxiv-econ", "https://rss.arxiv.org/rss/econ", "arxiv.org"),
    # SSRN Financial Economics Network — pre-publication finance research RSS.
    ("ssrn-fen", "https://www.ssrn.com/index.cfm/en/rss/recent/?networkName=fen", "ssrn.com"),
    # ── Primary US rulemaking dockets ───────────────────────────────────────
    # Federal Register documents tagged to the Treasury Dept (economy/finance
    # rules) as a documented RSS endpoint of the public FR API.
    (
        "federal-register-treasury",
        "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bagencies%5D%5B%5D=treasury-department",
        "federalregister.gov",
    ),
    # Regulations.gov newest documents (public RSS).
    ("regulations-gov", "https://www.regulations.gov/rss", "regulations.gov"),
    # ── Contrarian / heterodox macro commentary (front-runs consensus) ──────
    ("naked-capitalism", "https://www.nakedcapitalism.com/feed", "nakedcapitalism.com"),
    ("wolf-street", "https://wolfstreet.com/feed/", "wolfstreet.com"),
    ("pragmatic-capitalism", "https://disciplinefunds.com/feed/", "disciplinefunds.com"),
    ("the-big-picture", "https://ritholtz.com/feed/", "ritholtz.com"),
    ("project-syndicate-economics", "https://www.project-syndicate.org/rss/section/economics", "project-syndicate.org"),
    ("bloomberg-opinion", "https://feeds.bloomberg.com/opinion/news.rss", "bloomberg.com"),
)


@register_feed_provider
def specialist_feeds() -> list[FeedSpec]:
    """Return the SPECIALIST / niche high-signal RSS/Atom feeds.

    Every feed uses the default "rss" parser, so the built-in RSS/Atom path
    ingests them unchanged through the same fetch → dedup → ingest pipeline.
    """
    return [
        FeedSpec(name=f"spec-{slug}", url=url, fallback_domain=fallback_domain)
        for slug, url, fallback_domain in _SPECIALIST_FEEDS
    ]
