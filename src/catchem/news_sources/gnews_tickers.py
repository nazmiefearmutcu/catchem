"""ENTITY-awareness source pack — per-mega-cap-ticker Google News feeds.

The curated `gnews-*` queries in DEFAULT_FEEDS watch broad *topics* (finance,
stocks, the Fed, inflation, …). This pack adds the complementary *entity*
dimension: one Google News search feed per mega-cap company / crypto major /
index, so the system actively watches the single most newsworthy names by
ticker. Each feed runs the standard RSS parser against Google News' search
endpoint, which surfaces every major publisher's coverage of a name within
minutes of publication.

Each query is built as ``"<Company> stock OR <TICKER>"`` (e.g.
``"Apple stock OR AAPL"``) so Google News returns both editorial coverage that
names the company and ticker-tagged market wires. Index/ETF entries drop the
"stock" framing where it reads oddly (e.g. ``"S&P 500 OR SPX"``).

URL shape (verbatim contract):
    https://news.google.com/rss/search?q=<quote_plus(query)>&hl=en-US&gl=US&ceid=US:en

Feed names are ``gnews-tkr-<symbol-lower>`` so they sort together and never
collide with the topic-level ``gnews-*`` feeds. `fallback_domain` is
``news.google.com`` (the RSS parser rewrites each item's domain to the real
publisher via the ``<source url=...>`` element); `parser` is the default
``"rss"``.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..news_poller import FeedSpec, register_feed_provider

# Google News search RSS endpoint. Region/language locked to US English so the
# result set matches the rest of the finance-oriented feed bag.
_GNEWS_SEARCH = "https://news.google.com/rss/search"
_GNEWS_SUFFIX = "&hl=en-US&gl=US&ceid=US:en"

# (symbol, query) pairs. `symbol` lower-cases into the feed name; `query` is the
# human-readable search string we URL-encode. ~30 of the most newsworthy
# tickers: the mega-cap equity complex, two crypto-treasury proxies, and the
# two headline US indices. Order is roughly market-cap / newsworthiness.
_TICKERS: tuple[tuple[str, str], ...] = (
    # ── Mega-cap tech / "Magnificent Seven" + adjacents
    ("AAPL", "Apple stock OR AAPL"),
    ("MSFT", "Microsoft stock OR MSFT"),
    ("NVDA", "Nvidia stock OR NVDA"),
    ("GOOGL", "Google stock OR Alphabet OR GOOGL"),
    ("AMZN", "Amazon stock OR AMZN"),
    ("META", "Meta stock OR Facebook OR META"),
    ("TSLA", "Tesla stock OR TSLA"),
    ("AVGO", "Broadcom stock OR AVGO"),
    ("AMD", "AMD stock OR Advanced Micro Devices"),
    ("INTC", "Intel stock OR INTC"),
    ("NFLX", "Netflix stock OR NFLX"),
    # ── Financials
    ("JPM", "JPMorgan stock OR JPM"),
    ("BRK", "Berkshire Hathaway stock OR BRK"),
    ("V", "Visa stock OR Visa Inc"),
    ("BAC", "Bank of America stock OR BAC"),
    # ── Healthcare
    ("LLY", "Eli Lilly stock OR LLY"),
    # ── Energy
    ("XOM", "Exxon stock OR ExxonMobil OR XOM"),
    # ── Consumer / industrials / media
    ("WMT", "Walmart stock OR WMT"),
    ("BA", "Boeing stock OR BA"),
    ("DIS", "Disney stock OR DIS"),
    ("KO", "Coca-Cola stock OR KO"),
    ("MCD", "McDonald's stock OR MCD"),
    ("NKE", "Nike stock OR NKE"),
    # ── Crypto majors / treasury proxies
    ("COIN", "Coinbase stock OR COIN"),
    ("MSTR", "MicroStrategy stock OR MSTR"),
    # ── More financials / payments
    ("MA", "Mastercard stock OR MA"),
    ("GS", "Goldman Sachs stock OR GS"),
    # ── Indices / ETFs
    ("SPX", "S&P 500 OR SPX OR SPY ETF"),
    ("NDX", "Nasdaq OR NDX OR QQQ ETF"),
)


def _build_url(query: str) -> str:
    """Assemble a Google News search RSS URL with the query URL-encoded.

    `quote_plus` encodes spaces as ``+`` and escapes the ``&``/``OR``/``&P``
    payload so the assembled URL is valid and the ``hl``/``gl``/``ceid``
    suffix params are never swallowed into the query string.
    """
    return f"{_GNEWS_SEARCH}?q={quote_plus(query)}{_GNEWS_SUFFIX}"


@register_feed_provider
def _gnews_ticker_feeds() -> list[FeedSpec]:
    """Contribute one Google News search feed per mega-cap ticker/index."""
    return [
        FeedSpec(
            name=f"gnews-tkr-{symbol.lower()}",
            url=_build_url(query),
            fallback_domain="news.google.com",
            parser="rss",
        )
        for symbol, query in _TICKERS
    ]
