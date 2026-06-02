"""Tests for the Hacker News (Algolia) full-text search source pack.

NO live network: every assertion runs against an in-memory SAMPLE JSON
fixture fed straight to `_parse_hn`, plus an `assemble_feeds()` call that
only triggers in-process registration (no HTTP).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from catchem.news_poller import ParsedItem, assemble_feeds, get_parser
from catchem.news_sources.hn_algolia import _build_url, _hn_algolia_feeds, _parse_hn

# A representative Algolia search_by_date envelope:
#   * a well-formed hit with an external `url` + epoch `created_at_i`,
#   * a hit using `story_title`/`story_url` (the alternate field names),
#   * a hit with NO url/story_url → must link to its HN item page via
#     objectID, attributed to news.ycombinator.com,
#   * a hit with only an ISO `created_at` (no epoch) → parsed to UTC,
#   * a hit with no title at all → must be skipped,
#   * one entry that isn't a dict → must be skipped.
SAMPLE_HN = {
    "hits": [
        {
            "title": "Fed Holds Rates Steady Amid Inflation Watch",
            "url": "https://www.example-news.com/markets/fed-holds-rates",
            "objectID": "11111",
            "created_at_i": 1748442600,  # 2025-05-28T14:30:00Z
            "created_at": "2025-05-28T14:30:00.000Z",
            "points": 142,
            "num_comments": 88,
        },
        {
            "story_title": "Global Stocks Rally on Strong Earnings",
            "story_url": "https://markets.foobar.co.uk/story/stocks-rally",
            "objectID": "22222",
            "created_at_i": 1748408700,  # 2025-05-28T04:45:00Z
        },
        {
            # Ask HN / text post: no external url → link to HN item page.
            "title": "Ask HN: Best resources to learn about the bond market?",
            "objectID": "33333",
            "created_at_i": 1748412000,
        },
        {
            # No epoch — must fall back to parsing the ISO created_at string.
            "title": "Bitcoin Surges Past Resistance",
            "url": "https://crypto.example.org/btc-surge",
            "objectID": "44444",
            "created_at": "2025-05-28T09:05:00Z",
        },
        {
            # No usable title → must be skipped.
            "url": "https://nowhere.example.com/x",
            "objectID": "55555",
            "created_at_i": 1748412000,
        },
        "this-is-not-a-dict",  # junk entry → must be skipped
    ]
}

SAMPLE_BYTES = json.dumps(SAMPLE_HN).encode("utf-8")


def test_parse_yields_expected_items_and_skips_malformed() -> None:
    items = _parse_hn(SAMPLE_BYTES, fallback_domain="news.ycombinator.com")

    # 4 valid hits; the title-less one and the junk string are skipped.
    assert len(items) == 4
    assert all(isinstance(it, ParsedItem) for it in items)
    titles = {it.title for it in items}
    assert "Fed Holds Rates Steady Amid Inflation Watch" in titles
    # HN search carries no body → title reused verbatim as text.
    assert all(it.text == it.title for it in items)


def test_first_item_fields_and_epoch_utc_timestamp() -> None:
    items = _parse_hn(SAMPLE_BYTES, fallback_domain="news.ycombinator.com")
    first = next(it for it in items if it.url.endswith("fed-holds-rates"))

    assert first.title == "Fed Holds Rates Steady Amid Inflation Watch"
    # domain derived from the external URL host, `www.` stripped.
    assert first.domain == "example-news.com"
    # created_at_i epoch parsed exactly, tz-aware UTC.
    assert first.published_ts.tzinfo is not None
    assert first.published_ts.utcoffset() == timedelta(0)
    assert first.published_ts == datetime(2025, 5, 28, 14, 30, 0, tzinfo=UTC)


def test_story_title_and_story_url_alternate_fields() -> None:
    items = _parse_hn(SAMPLE_BYTES, fallback_domain="news.ycombinator.com")
    alt = next(it for it in items if it.title == "Global Stocks Rally on Strong Earnings")
    # story_url honored; domain derived from its host.
    assert alt.url == "https://markets.foobar.co.uk/story/stocks-rally"
    assert alt.domain == "markets.foobar.co.uk"


def test_missing_url_falls_back_to_hn_item_page() -> None:
    items = _parse_hn(SAMPLE_BYTES, fallback_domain="news.ycombinator.com")
    ask = next(it for it in items if it.title.startswith("Ask HN"))
    # No external url/story_url → canonical HN discussion page via objectID.
    assert ask.url == "https://news.ycombinator.com/item?id=33333"
    assert ask.domain == "news.ycombinator.com"


def test_iso_created_at_parsed_when_epoch_absent() -> None:
    items = _parse_hn(SAMPLE_BYTES, fallback_domain="news.ycombinator.com")
    btc = next(it for it in items if it.url.endswith("btc-surge"))
    # created_at "2025-05-28T09:05:00Z" parsed to tz-aware UTC.
    assert btc.published_ts.tzinfo is not None
    assert btc.published_ts.utcoffset() == timedelta(0)
    assert btc.published_ts == datetime(2025, 5, 28, 9, 5, 0, tzinfo=UTC)


def test_every_item_has_utc_published_ts() -> None:
    items = _parse_hn(SAMPLE_BYTES, fallback_domain="news.ycombinator.com")
    for it in items:
        assert it.published_ts.tzinfo is not None
        assert it.published_ts.utcoffset() == timedelta(0)


def test_title_less_hits_are_skipped() -> None:
    body = json.dumps({"hits": [
        {"url": "https://x.example.com/a", "objectID": "1"},  # no title
        {"title": "   ", "url": "https://x.example.com/b", "objectID": "2"},  # blank
        {"title": "good one", "url": "https://ok.example.com/x", "objectID": "3"},
    ]}).encode("utf-8")
    items = _parse_hn(body, fallback_domain="news.ycombinator.com")
    assert len(items) == 1
    assert items[0].title == "good one"


def test_invalid_json_returns_empty_list() -> None:
    assert _parse_hn(b"not json at all{", fallback_domain="news.ycombinator.com") == []
    assert _parse_hn(b"", fallback_domain="news.ycombinator.com") == []
    # valid JSON but wrong shape (no "hits" list) → []
    assert _parse_hn(b'{"status":"ok"}', fallback_domain="news.ycombinator.com") == []
    assert _parse_hn(b"[1, 2, 3]", fallback_domain="news.ycombinator.com") == []


def test_malformed_created_at_falls_back_to_now_utc() -> None:
    before = datetime.now(UTC)
    body = json.dumps({"hits": [
        {"title": "no timestamp here", "url": "https://x.example.com/a", "objectID": "1",
         "created_at": "not-a-real-date"},
    ]}).encode("utf-8")
    items = _parse_hn(body, fallback_domain="news.ycombinator.com")
    after = datetime.now(UTC)
    assert len(items) == 1
    ts = items[0].published_ts
    assert ts.tzinfo is not None
    assert before <= ts <= after


def test_provider_returns_hn_algolia_feedspecs() -> None:
    feeds = _hn_algolia_feeds()
    assert len(feeds) >= 6
    for spec in feeds:
        assert spec.parser == "hn_algolia"
        assert spec.fallback_domain == "news.ycombinator.com"
        assert spec.name.startswith("hn-algolia-")
        # URL must be properly encoded — no raw spaces leak through.
        assert " " not in spec.url
        assert "hn.algolia.com" in spec.url
        assert "tags=story" in spec.url
        assert "search_by_date" in spec.url


def test_build_url_encodes_boolean_query() -> None:
    url = _build_url("markets OR economy")
    assert " " not in url
    # quote_plus encodes the space as "+".
    assert "markets+OR+economy" in url


def test_hn_algolia_parser_registered() -> None:
    # The pack registers "hn_algolia" at import time; get_parser must resolve
    # it to our function (not silently fall back to the rss parser).
    parser = get_parser("hn_algolia")
    assert parser is _parse_hn


def test_feeds_appear_in_assemble_feeds() -> None:
    # assemble_feeds() imports catchem.news_sources, which auto-discovers and
    # imports this pack, firing its registration. No network involved.
    feeds = assemble_feeds()
    hn_feeds = [f for f in feeds if f.parser == "hn_algolia"]
    assert hn_feeds, "expected at least one hn_algolia-parser feed in assemble_feeds()"
    assert any(f.name.startswith("hn-algolia-") for f in hn_feeds)
    # The named stocks feed specifically should be present.
    assert any(f.name == "hn-algolia-stocks" for f in feeds)


def test_hn_algolia_edge_cases() -> None:
    from catchem.news_sources.hn_algolia import _parse_hn, _resolve_domain

    # 1. created_at_i is a boolean (must fall back to created_at or now())
    items1 = _parse_hn(
        json.dumps({"hits": [{"title": "Test Boolean", "url": "https://foo.com", "created_at_i": True, "created_at": "2025-05-28T14:30:00Z"}]}).encode("utf-8"),
        "fallback"
    )
    assert len(items1) == 1
    assert items1[0].published_ts == datetime(2025, 5, 28, 14, 30, 0, tzinfo=UTC)

    # 2. created_at_i is integer overflowing (must fall back to ISO or now())
    items2 = _parse_hn(
        json.dumps({"hits": [{"title": "Test Overflow", "url": "https://foo.com", "created_at_i": 999999999999999, "created_at": "2025-05-28T14:30:00Z"}]}).encode("utf-8"),
        "fallback"
    )
    assert len(items2) == 1
    assert items2[0].published_ts == datetime(2025, 5, 28, 14, 30, 0, tzinfo=UTC)

    # 3. Hostname parsing exception & empty hostname
    domain1 = _resolve_domain("http://[::1]abc", is_item_page=False)
    assert domain1 == "news.ycombinator.com"

    domain2 = _resolve_domain("/foo/bar", is_item_page=False)
    assert domain2 == "news.ycombinator.com"

    # 4. objectID is missing/empty/None when url is absent
    items3 = _parse_hn(
        json.dumps({"hits": [
            {"title": "Missing ID and URL"}, # skips
            {"title": "Empty ID and URL", "objectID": "   "}, # skips
            {"title": "Null ID and URL", "objectID": None}, # skips
            {"title": "Valid ID", "objectID": "123"}, # parses
        ]}).encode("utf-8"),
        "fallback"
    )
    assert len(items3) == 1
    assert items3[0].title == "Valid ID"

