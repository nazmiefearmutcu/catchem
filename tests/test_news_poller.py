"""Tests for the background RSS/Atom poller.

We don't make real network calls in CI — the poller is gated by
FUSION_NEWS__POLLER_ENABLED=false in the test env. These tests pin the
pure-function helpers: feed parsing and the small dedup cache.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fusion_stack.news_poller import (
    DEFAULT_FEEDS,
    FeedSpec,
    NewsPoller,
    ParsedItem,
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
    assert a.published_ts == datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
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
    assert eight_k.published_ts == datetime(2026, 5, 16, 13, 0, tzinfo=timezone.utc)


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


# ──────────────────────────────────────────────────────────────────────────────
# _SeenCache
# ──────────────────────────────────────────────────────────────────────────────


def test_seen_cache_dedup_and_lru_eviction() -> None:
    cache = _SeenCache(capacity=3)
    cache.add("a"); cache.add("b"); cache.add("c")
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


def test_news_poller_floors_interval_to_10s() -> None:
    """A misconfigured interval shouldn't hammer publishers."""
    class _StubSupervisor: pass
    class _StubSettings:
        class paths:
            from pathlib import Path
            fusion_output_dir = Path("/tmp")
    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=[FeedSpec("test", "https://example.com/rss")],
        interval_seconds=2.0,  # too aggressive
    )
    assert poller._interval == 10.0


def test_news_poller_status_fields_start_zeroed() -> None:
    class _StubSupervisor: pass
    class _StubSettings:
        class paths:
            from pathlib import Path
            fusion_output_dir = Path("/tmp")
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
