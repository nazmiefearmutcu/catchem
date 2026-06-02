"""Contract tests for the X / Twitter (Nitter RSS) social source pack.

Pure-unit + offline: a hand-built Nitter RSS 2.0 byte fixture is fed directly
to ``_parse_twitter`` (no network), and the registered feed provider is
verified to surface in ``assemble_feeds()`` with the right parser key. Nitter
emits ordinary RSS 2.0, so the fixture mirrors a real ``/<account>/rss`` body;
malformed bytes are asserted to yield ``[]`` (the never-raise contract).
"""

from __future__ import annotations

from datetime import UTC, datetime

from catchem.news_poller import ParsedItem, assemble_feeds
from catchem.news_sources.x_twitter import (
    _ACCOUNTS,
    _NITTER_BASE,
    _parse_twitter,
    _rewrite_to_x,
)

# ── Sample Nitter RSS 2.0 fixture ─────────────────────────────────────────
# Shape mirrors a real nitter /<account>/rss body: <rss><channel><item>...
# Three items exercise the rewrite + skip branches:
#   [0] permalink on nitter.net with a trailing "#m" media fragment
#   [1] permalink on a *different* nitter instance (nitter.poast.org)
#       pointing at the SAME tweet id as [0] would → both rewrite to the
#       identical x.com URL (instance-independent dedup key)
#   [2] item whose <link> is a bare fragment (no scheme/host) → unusable,
#       must be skipped after rewrite leaves it non-https
# pubDate is RFC-822; parse_feed → _parse_ts yields tz-aware UTC.
_SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DeItaone / @DeItaone</title>
    <link>https://nitter.net/DeItaone</link>
    <description>Twitter feed for: @DeItaone</description>
    <item>
      <title>*FED'S POWELL: RATES TO STAY HIGHER FOR LONGER</title>
      <link>https://nitter.net/DeItaone/status/1790000000000000001#m</link>
      <description>Walter Bloomberg headline body text here.</description>
      <pubDate>Tue, 28 May 2024 14:00:00 GMT</pubDate>
      <guid>https://nitter.net/DeItaone/status/1790000000000000001#m</guid>
    </item>
    <item>
      <title>*BREAKING: CPI COMES IN HOT</title>
      <link>https://nitter.poast.org/DeItaone/status/1790000000000000002</link>
      <description>Second tweet from a different nitter instance.</description>
      <pubDate>Tue, 28 May 2024 15:30:00 GMT</pubDate>
      <guid>https://nitter.poast.org/DeItaone/status/1790000000000000002</guid>
    </item>
    <item>
      <title>Unusable link tweet</title>
      <link>/DeItaone/status/relative-only</link>
      <description>This item has a non-absolute link and must be skipped.</description>
      <pubDate>Tue, 28 May 2024 16:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


# ── Parser behaviour ───────────────────────────────────────────────────────


def test_parse_twitter_rewrites_links_to_x_and_pins_domain() -> None:
    items = _parse_twitter(_SAMPLE_RSS, "x.com")

    # Two well-formed items survive; the relative-link item is skipped.
    assert len(items) == 2
    assert all(isinstance(i, ParsedItem) for i in items)

    first, second = items

    # ── item 0: nitter.net + "#m" fragment → canonical x.com, fragment dropped
    assert first.title == "*FED'S POWELL: RATES TO STAY HIGHER FOR LONGER"
    assert first.text == "Walter Bloomberg headline body text here."
    assert first.url == "https://x.com/DeItaone/status/1790000000000000001"
    assert "nitter" not in first.url
    assert "#m" not in first.url
    assert first.domain == "x.com"
    # RFC-822 pubDate → tz-aware UTC datetime
    assert first.published_ts == datetime(2024, 5, 28, 14, 0, 0, tzinfo=UTC)
    assert first.published_ts.tzinfo is not None
    assert first.published_ts.utcoffset() == UTC.utcoffset(None)

    # ── item 1: a DIFFERENT nitter instance rewrites onto the same x.com host
    assert second.url == "https://x.com/DeItaone/status/1790000000000000002"
    assert second.domain == "x.com"
    assert second.published_ts == datetime(2024, 5, 28, 15, 30, 0, tzinfo=UTC)


def test_parse_twitter_all_items_attribute_to_x_domain() -> None:
    items = _parse_twitter(_SAMPLE_RSS, "x.com")
    assert items, "fixture should yield at least one item"
    assert all(i.domain == "x.com" for i in items)
    assert all(i.url.startswith("https://x.com/") for i in items)


def test_rewrite_to_x_swaps_host_keeps_path_drops_fragment() -> None:
    assert (
        _rewrite_to_x("https://nitter.net/DeItaone/status/123#m")
        == "https://x.com/DeItaone/status/123"
    )
    # already-x.com link is left structurally equivalent (host forced https x.com)
    assert (
        _rewrite_to_x("https://x.com/markets/status/9")
        == "https://x.com/markets/status/9"
    )
    # a bare/relative reference can't be confidently rewritten → returned as-is
    assert _rewrite_to_x("/markets/status/9") == "/markets/status/9"
    assert _rewrite_to_x("") == ""


def test_parse_twitter_tolerates_garbage_bytes() -> None:
    """Non-XML / empty / unexpected bodies return [] rather than raising."""
    assert _parse_twitter(b"this is not xml", "x.com") == []
    assert _parse_twitter(b"", "x.com") == []
    assert _parse_twitter(b"<rss><channel></channel></rss>", "x.com") == []
    assert _parse_twitter(b"<<<broken", "x.com") == []


# ── Feed-provider registration ───────────────────────────────────────────────


def test_x_twitter_feeds_present_in_assemble_feeds() -> None:
    feeds = assemble_feeds()
    by_name = {f.name: f for f in feeds}

    for account in _ACCOUNTS:
        name = f"x-{account.lower()}"
        assert name in by_name, f"missing feed {name} from assemble_feeds()"
        spec = by_name[name]
        assert spec.parser == "twitter"
        assert spec.fallback_domain == "x.com"
        assert spec.url == f"{_NITTER_BASE}/{account}/rss"


def test_required_accounts_are_configured() -> None:
    """Pin the exact required X account set from the task spec."""
    assert set(_ACCOUNTS) == {
        "DeItaone",
        "FirstSquawk",
        "LiveSquawk",
        "financialjuice",
        "unusual_whales",
        "zerohedge",
        "markets",
        "CNBC",
        "ReutersBiz",
        "federalreserve",
        "SECGov",
        "WatcherGuru",
    }


def test_provider_returns_twelve_unique_feeds() -> None:
    from catchem.news_sources.x_twitter import _x_twitter_feeds

    specs = list(_x_twitter_feeds())
    assert len(specs) == 12
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), f"duplicate names: {names}"
    for spec in specs:
        assert spec.parser == "twitter"
        assert spec.fallback_domain == "x.com"
        assert spec.url.startswith("https://")


def test_rewrite_to_x_handles_invalid_url_gracefully() -> None:
    # A URL that is invalid and causes urlsplit to raise ValueError.
    bad_url = "http://[::1/status/123"
    assert _rewrite_to_x(bad_url) == bad_url


def test_parse_twitter_handles_parse_feed_exception(monkeypatch) -> None:
    import catchem.news_sources.x_twitter as xt

    def mock_parse_feed(body, fallback_domain):
        raise RuntimeError("simulated parse feed exception")

    monkeypatch.setattr(xt, "parse_feed", mock_parse_feed)
    assert _parse_twitter(b"dummy_rss", "x.com") == []

