"""Tests for the background RSS/Atom poller.

We don't make real network calls in CI — the poller is gated by
CATCHEM_NEWS__POLLER_ENABLED=false in the test env. These tests pin the
pure-function helpers: feed parsing and the small dedup cache.
"""

from __future__ import annotations

from datetime import UTC, datetime

from catchem.news_poller import (
    DEFAULT_FEEDS,
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
    _is_stale_published_ts,
    _SeenCache,
    parse_feed,
)

# ──────────────────────────────────────────────────────────────────────────────
# parse_feed
# ──────────────────────────────────────────────────────────────────────────────

RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Sample Business News</title>
    <item>
      <title>Markets jump on rate cut hopes</title>
      <link>https://example.com/news/markets-jump-rate-cut</link>
      <pubDate>Wed, 15 May 2026 14:00:00 +0000</pubDate>
      <description>&lt;p&gt;The S&amp;amp;P 500 climbed 1.2% as traders priced in a faster easing cycle.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Federal Reserve releases meeting minutes</title>
      <link>https://example.com/news/fed-minutes</link>
      <pubDate>Wed, 15 May 2026 18:30:00 +0000</pubDate>
      <content:encoded><![CDATA[<p>The FOMC minutes signaled <b>continued patience</b> on inflation.</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""


ATOM_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>SEC Filings (current)</title>
  <entry>
    <title>SCHEDULE 13G - Example Capital Partners</title>
    <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1234/0001234.htm" />
    <published>2026-05-16T12:34:00-04:00</published>
    <updated>2026-05-16T12:34:00-04:00</updated>
    <summary>Beneficial-ownership filing covering 6.7% of Acme Corp common stock.</summary>
  </entry>
  <entry>
    <title>8-K - Acme Corp</title>
    <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/9999/0008-k.htm" />
    <updated>2026-05-16T13:00:00Z</updated>
    <content type="html">Material announcement regarding ratings change.</content>
  </entry>
</feed>
"""


def test_parse_feed_handles_rss_with_html_descriptions() -> None:
    items = parse_feed(RSS_SAMPLE, fallback_domain="example.com")
    assert len(items) == 2

    a = items[0]
    assert a.title == "Markets jump on rate cut hopes"
    assert a.url == "https://example.com/news/markets-jump-rate-cut"
    # HTML entities decoded, tags stripped, whitespace collapsed.
    assert a.text == "The S&P 500 climbed 1.2% as traders priced in a faster easing cycle."
    # pubDate parsed to UTC.
    assert a.published_ts == datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
    assert a.domain == "example.com"

    b = items[1]
    # content:encoded preferred over <description> when both exist; CDATA unwrapped.
    assert "continued patience" in b.text
    # <b> tags stripped.
    assert "<b>" not in b.text


def test_parse_feed_handles_atom_entries() -> None:
    items = parse_feed(ATOM_SAMPLE, fallback_domain="sec.gov")
    assert len(items) == 2
    titles = [i.title for i in items]
    assert "SCHEDULE 13G - Example Capital Partners" in titles
    assert "8-K - Acme Corp" in titles
    # All items domain-resolved from link, not fallback.
    assert all(i.domain == "sec.gov" for i in items)
    # The 8-K row uses <updated> when <published> is absent.
    eight_k = next(i for i in items if i.title.startswith("8-K"))
    assert eight_k.published_ts == datetime(2026, 5, 16, 13, 0, tzinfo=UTC)


def test_parse_feed_drops_items_missing_required_fields() -> None:
    body = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>No link here</title><description>Body</description></item>
      <item><link>https://example.com/no-title</link><description>Body</description></item>
      <item><title>No body</title><link>https://example.com/x</link></item>
    </channel></rss>"""
    # Last item has title + link but no description; that's allowed because
    # text falls back to the title.
    items = parse_feed(body, fallback_domain="example.com")
    titles = [i.title for i in items]
    assert "No link here" not in titles  # missing link → dropped
    assert "No body" in titles           # falls back to title


def test_parse_feed_returns_empty_on_malformed_xml() -> None:
    assert parse_feed(b"<not xml>>>", fallback_domain="x.com") == []


def test_parse_feed_strips_www_from_domain_resolution() -> None:
    body = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Story</title>
        <link>https://www.bbc.co.uk/news/business-1</link>
        <description>Body</description>
      </item>
    </channel></rss>"""
    items = parse_feed(body, fallback_domain="bbc.com")
    assert items[0].domain == "bbc.co.uk"


def test_parse_feed_uses_google_news_source_domain() -> None:
    body = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Harvard Dumps Its Ethereum and Bitcoin ETF Investment - Yahoo Finance</title>
        <link>https://news.google.com/rss/articles/abc?oc=5</link>
        <pubDate>Sun, 17 May 2026 10:37:09 GMT</pubDate>
        <description>Harvard Dumps Its Ethereum and Bitcoin ETF Investment Yahoo Finance</description>
        <source url="https://finance.yahoo.com">Yahoo Finance</source>
      </item>
    </channel></rss>"""
    items = parse_feed(body, fallback_domain="news.google.com")
    assert len(items) == 1
    assert items[0].domain == "finance.yahoo.com"
    assert items[0].title == "Harvard Dumps Its Ethereum and Bitcoin ETF Investment"


def test_stale_item_filter_uses_max_age_seconds() -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    assert _is_stale_published_ts(
        datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        now,
        max_age_seconds=14 * 24 * 3600,
    ) is True
    assert _is_stale_published_ts(
        datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        now,
        max_age_seconds=14 * 24 * 3600,
    ) is False


# ──────────────────────────────────────────────────────────────────────────────
# _SeenCache
# ──────────────────────────────────────────────────────────────────────────────


def test_seen_cache_dedup_and_lru_eviction() -> None:
    cache = _SeenCache(capacity=3)
    cache.add("a")
    cache.add("b")
    cache.add("c")
    assert "a" in cache
    assert "b" in cache
    assert "c" in cache
    # Touching 'a' moves it to most-recent; adding 'd' evicts 'b' (the LRU
    # of the three after the touch).
    assert "a" in cache  # touch
    cache.add("d")
    assert "a" in cache
    assert "c" in cache
    assert "d" in cache
    assert "b" not in cache


# ── URL canonicalization for dedup ─────────────────────────────────────────
#
# Pre-fix: `_SeenCache` keyed on the raw URL, so the same article reached
# through tracking parameters or `www.` would slip past dedup and re-trigger
# the deterministic-id + storage round-trip. Storage still won the race, so
# this never produced duplicate records — but it added wasted work that
# scaled with feed count.

from catchem.news_poller import _canonical_url  # noqa: E402


def test_canonical_url_strips_www_prefix_for_dedup() -> None:
    assert _canonical_url("https://www.reuters.com/article/x") == _canonical_url(
        "https://reuters.com/article/x"
    )


def test_canonical_url_strips_utm_tracking_params() -> None:
    base = _canonical_url("https://cnbc.com/news/abc")
    assert _canonical_url("https://cnbc.com/news/abc?utm_source=feed") == base
    assert _canonical_url("https://cnbc.com/news/abc?utm_source=feed&utm_medium=rss") == base
    # Real query params (not tracking) must be preserved.
    assert _canonical_url("https://cnbc.com/news/abc?id=42") != base


def test_canonical_url_strips_trailing_slash() -> None:
    assert _canonical_url("https://bbc.com/news/x/") == _canonical_url("https://bbc.com/news/x")
    # But root path stays as "/" so we don't collapse the origin to ""
    assert _canonical_url("https://bbc.com/") == _canonical_url("https://bbc.com")


def test_canonical_url_lowercases_host_but_keeps_path_case() -> None:
    assert _canonical_url("https://ReutERS.com/Article/X") == _canonical_url(
        "https://reuters.com/Article/X"
    )
    # Different path case is a different URL — publishers use case-sensitive paths.
    assert _canonical_url("https://reuters.com/article/X") != _canonical_url(
        "https://reuters.com/article/x"
    )


def test_canonical_url_falls_back_on_unparseable_input() -> None:
    # Bizarre input must not raise — return as-is so the cache still tracks
    # the literal string (defense in depth).
    assert _canonical_url("") == ""
    assert _canonical_url("not a url") == "not a url"


def test_seen_cache_dedups_tracking_param_variants_after_canonicalization() -> None:
    cache = _SeenCache(capacity=8)
    cache.add(_canonical_url("https://cnbc.com/news/x"))
    assert _canonical_url("https://cnbc.com/news/x?utm_source=feed") in cache
    assert _canonical_url("https://www.cnbc.com/news/x?utm_campaign=daily") in cache


# ──────────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────────


def test_default_feeds_cover_a_diverse_source_set() -> None:
    names = {f.name for f in DEFAULT_FEEDS}
    # At least one each from: regulators, business news, crypto.
    assert any("fed" in n or "sec" in n for n in names), names
    assert any("bbc" in n or "cnbc" in n or "reuters" in n for n in names), names
    assert any("coindesk" in n or "crypto" in n for n in names), names


def test_default_feeds_have_valid_https_urls() -> None:
    for spec in DEFAULT_FEEDS:
        assert isinstance(spec, FeedSpec)
        # All but one are https; the BBC one is http (the BBC redirects to https
        # but the canonical RSS URL is http). Accept either.
        assert spec.url.startswith(("http://", "https://"))


# ──────────────────────────────────────────────────────────────────────────────
# NewsPoller construction
# ──────────────────────────────────────────────────────────────────────────────


def test_default_poll_interval_matches_ui_fallback() -> None:
    """If this default ever moves, frontend FeedPage interval fallback (?? 10)
    and the surrounding 'every 10s' comment must move in lockstep — otherwise
    the UI will silently lie about cadence the first time /ui/news-status is
    not yet populated (cold boot, brief race).
    See Round 6 Bug 3."""
    from catchem.settings import NewsConfig
    assert NewsConfig().poll_interval_seconds == 10.0


def test_news_poller_floors_interval_to_10s() -> None:
    """A misconfigured interval shouldn't hammer publishers."""
    class _StubSupervisor:
        pass
    class _StubSettings:
        class paths:
            from pathlib import Path
            catchem_output_dir = Path("/tmp")
    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=[FeedSpec("test", "https://example.com/rss")],
        interval_seconds=2.0,  # too aggressive
    )
    assert poller._interval == 10.0


def test_news_poller_status_fields_start_zeroed() -> None:
    class _StubSupervisor:
        pass
    class _StubSettings:
        class paths:
            from pathlib import Path
            catchem_output_dir = Path("/tmp")
    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=[],
    )
    assert poller.last_run_at is None
    assert poller.last_ingested == 0
    assert poller.total_ingested == 0
    assert poller.last_error is None
    # New "healthy-but-quiet" tracking fields. The UI uses these to show
    # the user that the poller is alive even when publishers are idle.
    assert poller.last_new_at is None
    assert poller.empty_ticks == 0
    assert poller.last_stale_skipped == 0
    assert poller._max_item_age_seconds == 14 * 24 * 3600


def test_news_poller_records_per_feed_health() -> None:
    class _StubSupervisor:
        pass
    class _StubSettings:
        class paths:
            from pathlib import Path
            catchem_output_dir = Path("/tmp")
    spec = FeedSpec("sample-feed", "https://example.com/rss", "example.com")
    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=[spec],
    )
    item = ParsedItem(
        title="Markets rally",
        text="Stocks rallied after earnings.",
        url="https://example.com/a",
        domain="example.com",
        published_ts=datetime(2026, 5, 15, 14, 0, tzinfo=UTC),
    )

    poller._record_feed_result(FeedFetchResult(spec=spec, items=(item,), status_code=200, elapsed_ms=12.0))
    healthy = poller.feed_health_snapshot()[0]
    assert healthy["ok"] is True
    assert healthy["item_count"] == 1
    assert healthy["consecutive_errors"] == 0

    poller._record_feed_result(FeedFetchResult(spec=spec, status_code=429, error="http_429", elapsed_ms=2.0))
    unhealthy = poller.feed_health_snapshot()[0]
    assert unhealthy["ok"] is False
    assert unhealthy["status_code"] == 429
    assert unhealthy["total_fetches"] == 2
    assert unhealthy["total_errors"] == 1
    assert unhealthy["consecutive_errors"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# Public read-only accessors (v34 MED 20)
# ──────────────────────────────────────────────────────────────────────────────

def _make_poller(**kw) -> NewsPoller:
    """Shared stub builder for the property-accessor tests."""
    class _StubSupervisor:
        pass

    class _StubSettings:
        class paths:
            from pathlib import Path
            catchem_output_dir = Path("/tmp")

    return NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        **kw,
    )


def test_feeds_property_returns_frozen_tuple() -> None:
    spec_a = FeedSpec("a", "https://example.com/a")
    spec_b = FeedSpec("b", "https://example.com/b")
    poller = _make_poller(feeds=[spec_a, spec_b])
    assert poller.feeds == (spec_a, spec_b)
    assert isinstance(poller.feeds, tuple)
    # Property is read-only — setattr should fail rather than silently
    # mutate internal config.
    try:
        poller.feeds = ()  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("expected feeds @property to be read-only")


def test_interval_seconds_property_reports_clamped_value() -> None:
    # Constructor floor is 10.0s; passing 2.0 must be promoted, and the
    # property must expose the clamped value (not the user-requested one).
    poller = _make_poller(feeds=[FeedSpec("a", "https://example.com/a")], interval_seconds=2.0)
    assert poller.interval_seconds == 10.0


def test_max_item_age_seconds_property_default() -> None:
    poller = _make_poller(feeds=[])
    # Default in __init__ is 14 days; property must surface that.
    assert poller.max_item_age_seconds == 14 * 24 * 3600
    # Custom value flows through and clamps negatives to zero.
    poller2 = _make_poller(feeds=[], max_item_age_seconds=-5.0)
    assert poller2.max_item_age_seconds == 0.0
