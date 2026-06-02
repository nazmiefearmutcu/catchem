"""Contract tests for the DYNAMIC operator-watchlist Google News source pack.

Pure / offline — no network. They pin:

  * with no `priority_tickers` configured, the provider falls back to the
    built-in mega-cap roster (>=10 feeds, all named ``gnews-watch-*``),
  * when the operator DOES configure tickers (via env + reload_settings), the
    provider emits exactly those, with correctly percent-encoded GN search URLs
    and the ``.`` → ``-`` feed-name normalization,
  * and the pack is auto-discovered into ``assemble_feeds()`` alongside
    DEFAULT_FEEDS without clobbering existing feeds.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, quote_plus, urlsplit

import pytest

from catchem.news_poller import FeedSpec, assemble_feeds
from catchem.news_sources.watchlist_dynamic import (
    _DEFAULT_TICKERS,
    _gnews_watchlist_feeds,
)
from catchem.settings import reload_settings


@pytest.fixture
def clean_settings_cache() -> Iterator[None]:
    """Drop the settings lru_cache before AND after so env overrides take
    effect and never leak into sibling tests."""
    reload_settings()
    try:
        yield
    finally:
        reload_settings()


def test_no_priority_tickers_falls_back_to_builtin_default(
    monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
) -> None:
    """Empty config → built-in mega-cap fallback (>=10 ``gnews-watch-*`` feeds)."""
    monkeypatch.delenv("CATCHEM_NEWS__PRIORITY_TICKERS", raising=False)
    reload_settings()

    feeds = list(_gnews_watchlist_feeds())
    assert len(feeds) >= 10, f"expected >=10 fallback feeds, got {len(feeds)}"

    names = [f.name for f in feeds]
    assert all(n.startswith("gnews-watch-") for n in names), names
    assert len(names) == len(set(names)), "feed names must be unique"

    # The fallback roster must be exactly the built-in default set.
    expected = {
        f"gnews-watch-{t.lower().replace('.', '-')}" for t in _DEFAULT_TICKERS
    }
    assert set(names) == expected
    # Spot-check the class-share normalization (BRK.B → brk-b).
    assert "gnews-watch-brk-b" in names

    # Every fallback feed is a well-formed GN search FeedSpec.
    for f in feeds:
        assert isinstance(f, FeedSpec)
        assert f.parser == "rss"
        assert f.fallback_domain == "news.google.com"


def test_configured_priority_tickers_emit_exactly_those(
    monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
) -> None:
    """Operator config (env) → exactly those tickers, correctly encoded."""
    monkeypatch.setenv("CATCHEM_NEWS__PRIORITY_TICKERS", '["NFLX","COIN"]')
    reload_settings()

    feeds = list(_gnews_watchlist_feeds())
    names = [f.name for f in feeds]
    assert names == ["gnews-watch-nflx", "gnews-watch-coin"], names

    by_name = {f.name: f for f in feeds}
    for ticker in ("NFLX", "COIN"):
        f = by_name[f"gnews-watch-{ticker.lower()}"]
        parts = urlsplit(f.url)
        assert parts.scheme == "https"
        assert parts.netloc == "news.google.com"
        assert parts.path == "/rss/search"
        qs = parse_qs(parts.query)
        assert qs.get("hl") == ["en-US"]
        assert qs.get("gl") == ["US"]
        assert qs.get("ceid") == ["US:en"]
        # Exact decoded query + verbatim encoded form in the raw URL.
        expected_query = f'"{ticker}" stock'
        assert qs.get("q") == [expected_query]
        assert f"q={quote_plus(expected_query)}" in f.url
        # No raw spaces survived into the URL.
        assert " " not in parts.query
        assert f.parser == "rss"
        assert f.fallback_domain == "news.google.com"


def test_configured_feeds_appear_in_assemble_feeds(
    monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
) -> None:
    """The pack is auto-discovered, so configured feeds surface in the merged
    bag — and DEFAULT_FEEDS still coexist without name collisions."""
    monkeypatch.setenv("CATCHEM_NEWS__PRIORITY_TICKERS", '["NFLX","COIN"]')
    reload_settings()

    assembled = assemble_feeds()
    assembled_names = {f.name for f in assembled}
    assert "gnews-watch-nflx" in assembled_names
    assert "gnews-watch-coin" in assembled_names
    # Topic-level DEFAULT_FEEDS feed still present.
    assert "gnews-finance" in assembled_names
    # Globally-unique names across the whole assembled bag.
    all_names = [f.name for f in assembled]
    assert len(all_names) == len(set(all_names))


def test_watchlist_dynamic_degrades_on_settings_exception(monkeypatch) -> None:
    import catchem.settings as settings_mod

    def mock_load_settings():
        raise RuntimeError("simulated settings load error")

    monkeypatch.setattr(settings_mod, "load_settings", mock_load_settings)
    # Fails to load settings → falls back to _DEFAULT_TICKERS
    feeds = list(_gnews_watchlist_feeds())
    assert len(feeds) == len(_DEFAULT_TICKERS)


def test_watchlist_normalization_drops_blanks_and_duplicates(
    monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
) -> None:
    # Sloppy priority list with blanks and duplicates (case-insensitive)
    monkeypatch.setenv("CATCHEM_NEWS__PRIORITY_TICKERS", '["AAPL", "", "  ", "AAPL", "aapl", "MSFT"]')
    reload_settings()

    feeds = list(_gnews_watchlist_feeds())
    names = [f.name for f in feeds]
    # AAPL (deduped) and MSFT
    assert names == ["gnews-watch-aapl", "gnews-watch-msft"]

