"""Contract tests for the ENTITY-awareness Google News ticker source pack.

These are pure / offline — no network. They pin:

  * the provider returns ~30 feeds, all named ``gnews-tkr-<symbol-lower>``
    with unique names,
  * each URL is a valid, properly-encoded Google News *search* RSS endpoint
    with the locked ``hl``/``gl``/``ceid`` suffix and an encoded ``q=`` param,
  * every feed uses the ``rss`` parser + ``news.google.com`` fallback domain,
  * and the pack actually shows up in ``assemble_feeds()`` (i.e. it is
    auto-discovered and registered) without clobbering the topic-level
    ``gnews-*`` feeds in DEFAULT_FEEDS.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote_plus, urlsplit

from catchem.news_poller import FeedSpec, assemble_feeds
from catchem.news_sources.gnews_tickers import _gnews_ticker_feeds


def test_provider_returns_about_thirty_unique_ticker_feeds() -> None:
    feeds = list(_gnews_ticker_feeds())
    # "~30" — give a sensible band so the exact roster can flex without the
    # test going stale, while still catching an accidental empty/half list.
    assert 20 <= len(feeds) <= 35, f"expected ~30 ticker feeds, got {len(feeds)}"
    names = [f.name for f in feeds]
    assert len(names) == len(set(names)), "feed names must be unique"


def test_every_feed_is_a_well_formed_ticker_feedspec() -> None:
    for f in _gnews_ticker_feeds():
        assert isinstance(f, FeedSpec)
        assert f.name.startswith("gnews-tkr-"), f.name
        # Suffix after the prefix is the lower-cased symbol — no spaces/upper.
        symbol = f.name[len("gnews-tkr-"):]
        assert symbol and symbol == symbol.lower() and " " not in symbol
        assert f.parser == "rss"
        assert f.fallback_domain == "news.google.com"


def test_urls_are_valid_encoded_google_news_search_endpoints() -> None:
    for f in _gnews_ticker_feeds():
        parts = urlsplit(f.url)
        assert parts.scheme == "https"
        assert parts.netloc == "news.google.com"
        assert parts.path == "/rss/search"
        qs = parse_qs(parts.query)
        # Locked region/language contract.
        assert qs.get("hl") == ["en-US"]
        assert qs.get("gl") == ["US"]
        assert qs.get("ceid") == ["US:en"]
        # A non-empty, URL-encoded query that mentions the symbol's ticker.
        q_values = qs.get("q")
        assert q_values and q_values[0]
        query = q_values[0]
        symbol = f.name[len("gnews-tkr-"):]
        assert symbol.upper() in query.upper()


def test_url_query_is_actually_url_encoded() -> None:
    """The raw URL must carry the percent/plus-encoded form, not raw spaces."""
    for f in _gnews_ticker_feeds():
        raw_query = urlsplit(f.url).query
        assert " " not in raw_query  # spaces must be encoded
        # Reconstruct the expected encoded q= from the decoded value and
        # confirm it appears verbatim in the raw URL.
        decoded = parse_qs(raw_query)["q"][0]
        assert f"q={quote_plus(decoded)}" in f.url


def test_known_marquee_tickers_present() -> None:
    names = {f.name for f in _gnews_ticker_feeds()}
    for symbol in ("aapl", "msft", "nvda", "tsla", "coin", "mstr"):
        assert f"gnews-tkr-{symbol}" in names, symbol


def test_feeds_are_discovered_in_assemble_feeds() -> None:
    """assemble_feeds() auto-imports catchem.news_sources, so every ticker
    feed must surface in the merged bag alongside DEFAULT_FEEDS."""
    assembled = assemble_feeds()
    assembled_names = {f.name for f in assembled}
    for f in _gnews_ticker_feeds():
        assert f.name in assembled_names, f"{f.name} missing from assemble_feeds()"
    # Sanity: the topic-level gnews feeds from DEFAULT_FEEDS still coexist.
    assert "gnews-finance" in assembled_names
    # Names remain globally unique across the whole assembled bag.
    all_names = [f.name for f in assembled]
    assert len(all_names) == len(set(all_names))
