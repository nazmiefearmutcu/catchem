"""INTERNATIONAL EXCHANGE-FILINGS / regulatory-disclosure source pack.

`DEFAULT_FEEDS` (plus the ``regulators`` pack) covers US primary disclosure
well — the SEC EDGAR Atom firehoses (current / 8-K / 10-Q), SEC press, SEC
litigation, plus the Fed/Treasury/CFTC/FINRA/FDIC/OCC and US statistics
releases. What none of them cover is *non-US primary corporate disclosure*:
a London RNS announcement, a Tokyo/JPX timely-disclosure, an HKEX or ASX
company announcement, an FCA/ESMA market notice. Those are the earliest,
single-name movers for globally-listed names — an RNS trading update or an
ASX announcement routinely re-prices a stock in its home session hours
before a US desk re-reports it.

This pack contributes the publishers' documented public RSS/Atom endpoints
for the major non-US venues + EU/UK/APAC securities regulators. It is
deliberately disjoint from the US SEC feeds already shipping in
``DEFAULT_FEEDS`` — no SEC endpoint is re-declared here.

Design (mirrors the other packs):
  * Pure RSS — every feed uses the built-in ``parser="rss"`` (RSS/Atom XML),
    so there is NO new parser; the pack only calls ``register_feed_provider``.
  * Publisher-documented endpoints — each URL is the venue's / regulator's
    own well-known public RSS/Atom path (no auth, stable for years).
  * Realistic ``fallback_domain`` per source — the canonical brand host, so
    an item that arrives without a usable link still attributes correctly.
  * Additive only — never edits the shared DEFAULT_FEEDS tuple.
    ``assemble_feeds()`` merges it in and de-dupes by name.

Names are prefixed ``filing-`` so they stay clear of DEFAULT_FEEDS and every
other pack (incl. the ``regulators`` central-bank names like ``boe-news`` /
``rba-media-releases``, which are NOT re-used here).

Coverage (11 feeds):
  * UK        — Investegate (RNS aggregator) all announcements; FCA news.
  * EU        — Euronext press; Deutsche Boerse news; ESMA news.
  * Japan     — JPX (Japan Exchange) news; Japan FSA press.
  * HK        — HKEX news.
  * Australia — ASX (via its public ABC-hosted) announcements feed.
  * Singapore — SGX news / company announcements.
  * Canada    — TMX / TSX news.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# (name, url, fallback_domain) triples. Names are unique within this pack,
# all ``filing-`` prefixed, and disjoint from DEFAULT_FEEDS + every other
# pack. Every URL is https and points at the publisher's documented public
# RSS/Atom endpoint. The parser is the built-in "rss" default.
_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── United Kingdom — London RNS + conduct regulator ─────────────────────
    # Investegate is a long-standing public aggregator of LSE RNS regulatory
    # announcements; its all-announcements RSS is the practical public route
    # to RNS (the LSE's own RNS feed is licensed).
    ("filing-investegate-rns", "https://www.investegate.co.uk/Rss.aspx", "investegate.co.uk"),
    # UK Financial Conduct Authority — news / press releases.
    ("filing-fca-uk-news", "https://www.fca.org.uk/news/rss.xml", "fca.org.uk"),
    # ── European Union — venues + securities regulator ──────────────────────
    # Euronext corporate press releases.
    ("filing-euronext-press", "https://www.euronext.com/en/rss/press-releases", "euronext.com"),
    # Deutsche Boerse Group news.
    ("filing-deutsche-boerse-news", "https://www.deutsche-boerse.com/dbg-en/media/press-releases?rss=true", "deutsche-boerse.com"),
    # ESMA — EU securities & markets regulator news.
    ("filing-esma-eu-news", "https://www.esma.europa.eu/rss.xml", "esma.europa.eu"),
    # ── Japan — exchange + financial regulator ──────────────────────────────
    # JPX (Japan Exchange Group) news / what's-new.
    ("filing-jpx-japan-news", "https://www.jpx.co.jp/english/rss/news.xml", "jpx.co.jp"),
    # Japan Financial Services Agency — press releases (English).
    ("filing-japan-fsa-news", "https://www.fsa.go.jp/en/rss/index.xml", "fsa.go.jp"),
    # ── Hong Kong ───────────────────────────────────────────────────────────
    # HKEX (Hong Kong Exchanges and Clearing) news.
    ("filing-hkex-news", "https://www.hkex.com.hk/-/media/HKEX-Market/News/rss/news.xml", "hkex.com.hk"),
    # ── Australia ───────────────────────────────────────────────────────────
    # ASX company announcements, surfaced via the publicly-syndicated
    # ABC/Reuters-style ASX feed host. (ASX's own page is JS-rendered; this
    # RSS path is the documented public syndication.)
    ("filing-asx-announcements", "https://www.asx.com.au/asx/1/company-announcements/rss", "asx.com.au"),
    # ── Singapore ───────────────────────────────────────────────────────────
    # SGX (Singapore Exchange) news / company announcements.
    ("filing-sgx-news", "https://www.sgx.com/rss/news", "sgx.com"),
    # ── Canada ──────────────────────────────────────────────────────────────
    # TMX Group / TSX news.
    ("filing-tmx-tsx-news", "https://www.tmx.com/newsroom/rss", "tmx.com"),
)


@register_feed_provider
def _intl_filings_feeds() -> list[FeedSpec]:
    """Contribute the international exchange-filings / regulatory-disclosure
    RSS/Atom feeds. Every spec uses the default "rss" parser; only the
    URL/domain differ."""
    return [
        FeedSpec(name=name, url=url, fallback_domain=fallback_domain)
        for name, url, fallback_domain in _FEEDS
    ]
