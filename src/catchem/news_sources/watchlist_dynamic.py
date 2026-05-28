"""DYNAMIC watchlist source pack — per-priority-ticker Google News feeds.

Where `gnews_tickers.py` ships a *static*, hand-curated roster of mega-cap
names, this pack is *operator-driven*: it reads ``settings.news.priority_tickers``
and emits one near-real-time Google News search feed per ticker the operator
actually configured. The result is that awareness tracks exactly the names the
desk cares about — not just the mainstream surface — and the roster changes the
moment the operator edits the setting (the provider is invoked at
``assemble_feeds()`` / poller-construction time, by which point settings exist).

If ``priority_tickers`` is empty (the default), the pack falls back to a built-in
set of ~15 mega-caps so the feed bag is never short of entity-level coverage.

URL shape (verbatim contract — identical to the static ticker pack so dedup and
the RSS parser behave the same):
    https://news.google.com/rss/search?q=<quote_plus(query)>&hl=en-US&gl=US&ceid=US:en

Each query is ``f'"{ticker}" stock'`` (e.g. ``'"AAPL" stock'``); the quotes pin
Google News to the literal symbol so a 4-letter ticker doesn't pull unrelated
hits. Feed names are ``gnews-watch-<symbol>`` where ``<symbol>`` is the ticker
lower-cased with ``.`` → ``-`` (so ``BRK.B`` → ``gnews-watch-brk-b``), keeping
names URL/identifier-safe and collision-free with the ``gnews-tkr-*`` and
topic-level ``gnews-*`` feeds. ``fallback_domain`` is ``news.google.com`` (the
RSS parser rewrites each item to the real publisher via ``<source url=...>``);
``parser`` is the default ``"rss"``.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..news_poller import FeedSpec, register_feed_provider

# Google News search RSS endpoint. Region/language locked to US English so the
# result set matches the rest of the finance-oriented feed bag.
_GNEWS_SEARCH = "https://news.google.com/rss/search"
_GNEWS_SUFFIX = "&hl=en-US&gl=US&ceid=US:en"

# Fallback roster used when the operator hasn't configured any priority tickers.
# ~15 of the largest, most-newsworthy US names so entity-level coverage is never
# empty out of the box. Kept deliberately broad (tech + financials + energy +
# staples + healthcare) rather than tech-heavy.
_DEFAULT_TICKERS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "BRK.B", "V",
    "XOM", "WMT", "AVGO", "LLY", "JNJ",
)


def _feed_name(ticker: str) -> str:
    """Derive a URL/identifier-safe feed name from a ticker.

    Lower-cases and maps ``.`` → ``-`` so class-share symbols like ``BRK.B``
    yield ``gnews-watch-brk-b`` (no dots in feed names, which keeps them clean
    as DOM ids / log keys).
    """
    return f"gnews-watch-{ticker.lower().replace('.', '-')}"


def _build_url(ticker: str) -> str:
    """Assemble a Google News search RSS URL for one ticker.

    Query is ``f'"{ticker}" stock'`` URL-encoded; ``quote_plus`` escapes the
    quotes and spaces so the ``hl``/``gl``/``ceid`` suffix params are never
    swallowed into the query string.
    """
    query = f'"{ticker}" stock'
    return f"{_GNEWS_SEARCH}?q={quote_plus(query)}{_GNEWS_SUFFIX}"


def _resolve_tickers() -> list[str]:
    """Return the operator's priority tickers, or the built-in fallback.

    Reading settings is wrapped in try/except so a misconfigured or
    unimportable settings layer degrades to the fallback roster rather than
    breaking ``assemble_feeds()`` (which already swallows provider errors, but
    a clean fallback keeps entity coverage alive instead of dropping it).
    """
    try:
        from ..settings import load_settings

        tickers = load_settings().news.priority_tickers
    except Exception:
        tickers = []
    # Normalize: strip whitespace, drop blanks, de-dupe (preserving order) so a
    # sloppy YAML/env list (`"AAPL, , aapl"`) yields clean, unique feeds.
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in tickers or []:
        sym = str(raw).strip()
        if not sym:
            continue
        key = sym.upper()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(sym)
    return cleaned or list(_DEFAULT_TICKERS)


@register_feed_provider
def _gnews_watchlist_feeds() -> list[FeedSpec]:
    """Contribute one Google News search feed per operator-priority ticker.

    Invoked at ``assemble_feeds()`` time (poller construction), after settings
    are loaded, so ``load_settings()`` here reflects the operator's config.
    """
    return [
        FeedSpec(
            name=_feed_name(ticker),
            url=_build_url(ticker),
            fallback_domain="news.google.com",
            parser="rss",
        )
        for ticker in _resolve_tickers()
    ]
