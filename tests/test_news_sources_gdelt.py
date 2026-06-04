"""Tests for the GDELT global-news firehose source pack.

NO live network: every assertion runs against an in-memory SAMPLE JSON
fixture fed straight to `_parse_gdelt`, plus an `assemble_feeds()` call that
only triggers in-process registration (no HTTP).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from catchem.news_poller import ParsedItem, assemble_feeds, get_parser
from catchem.news_sources.gdelt import _gdelt_feeds, _parse_gdelt

# A representative DOC 2.0 ArtList envelope:
#   * two well-formed articles (one with an explicit `domain`, one without —
#     so the URL-derived path is exercised),
#   * one article with a malformed seendate (must fall back to now/UTC),
#   * one article with NO url (must be skipped),
#   * one entry that isn't even a dict (must be skipped).
SAMPLE_GDELT = {
    "articles": [
        {
            "url": "https://www.example-news.com/markets/fed-holds-rates",
            "title": "Fed Holds Rates Steady Amid Inflation Watch",
            "seendate": "20260528T143000Z",
            "domain": "example-news.com",
            "sourcecountry": "United States",
        },
        {
            "url": "https://markets.foobar.co.uk/story/stocks-rally",
            "title": "Global Stocks Rally on Strong Earnings",
            "seendate": "20260528T090500Z",
            # no `domain` key → derive from the URL host
            "sourcecountry": "United Kingdom",
        },
        {
            "url": "https://crypto.example.org/btc-surge",
            "title": "Bitcoin Surges Past Resistance",
            "seendate": "not-a-real-date",  # malformed → now(UTC) fallback
            "domain": "crypto.example.org",
        },
        {
            # malformed: missing url → must be skipped
            "title": "Headline With No Link",
            "seendate": "20260528T120000Z",
            "domain": "nowhere.example.com",
        },
        "this-is-not-a-dict",  # junk entry → must be skipped
    ]
}

SAMPLE_BYTES = json.dumps(SAMPLE_GDELT).encode("utf-8")


def test_parse_yields_expected_items_and_skips_malformed() -> None:
    items = _parse_gdelt(SAMPLE_BYTES, fallback_domain="gdelt.org")

    # 3 valid articles; the url-less one and the junk string are skipped.
    assert len(items) == 3
    assert all(isinstance(it, ParsedItem) for it in items)
    urls = {it.url for it in items}
    assert "https://www.example-news.com/markets/fed-holds-rates" in urls
    assert all("Headline With No Link" not in it.title for it in items)


def test_first_item_fields_and_utc_timestamp() -> None:
    items = _parse_gdelt(SAMPLE_BYTES, fallback_domain="gdelt.org")
    first = next(it for it in items if it.url.endswith("fed-holds-rates"))

    assert first.title == "Fed Holds Rates Steady Amid Inflation Watch"
    # GDELT has no body → title is reused verbatim as text.
    assert first.text == first.title
    # explicit `domain` field is honored, `www.` stripped where present.
    assert first.domain == "example-news.com"
    # seendate "20260528T143000Z" parsed exactly, tz-aware UTC.
    assert first.published_ts.tzinfo is not None
    assert first.published_ts.utcoffset() == timedelta(0)
    assert first.published_ts == datetime(2026, 5, 28, 14, 30, 0, tzinfo=UTC)


def test_domain_derived_from_url_when_absent() -> None:
    items = _parse_gdelt(SAMPLE_BYTES, fallback_domain="gdelt.org")
    derived = next(it for it in items if it.url.endswith("stocks-rally"))
    # no `domain` key → host parsed from URL (no `www.` here).
    assert derived.domain == "markets.foobar.co.uk"


def test_malformed_seendate_falls_back_to_now_utc() -> None:
    before = datetime.now(UTC)
    items = _parse_gdelt(SAMPLE_BYTES, fallback_domain="gdelt.org")
    after = datetime.now(UTC)
    btc = next(it for it in items if it.url.endswith("btc-surge"))

    assert btc.published_ts.tzinfo is not None
    # Fallback timestamp is "now", i.e. within the call window.
    assert before <= btc.published_ts <= after


def test_every_item_has_utc_published_ts() -> None:
    items = _parse_gdelt(SAMPLE_BYTES, fallback_domain="gdelt.org")
    for it in items:
        assert it.published_ts.tzinfo is not None
        assert it.published_ts.utcoffset() == timedelta(0)


def test_invalid_json_returns_empty_list() -> None:
    assert _parse_gdelt(b"not json at all{", fallback_domain="gdelt.org") == []
    assert _parse_gdelt(b"", fallback_domain="gdelt.org") == []
    # valid JSON but wrong shape (no "articles" list) → []
    assert _parse_gdelt(b'{"status":"ok"}', fallback_domain="gdelt.org") == []
    assert _parse_gdelt(b"[1, 2, 3]", fallback_domain="gdelt.org") == []


def test_url_less_articles_are_skipped() -> None:
    body = json.dumps({"articles": [
        {"title": "no url here", "seendate": "20260528T120000Z"},
        {"url": "   ", "title": "blank url"},
        {"url": "https://ok.example.com/x", "title": "good one"},
    ]}).encode("utf-8")
    items = _parse_gdelt(body, fallback_domain="gdelt.org")
    assert len(items) == 1
    assert items[0].url == "https://ok.example.com/x"


def test_provider_returns_gdelt_feedspecs() -> None:
    feeds = _gdelt_feeds()
    assert len(feeds) >= 3
    for spec in feeds:
        assert spec.parser == "gdelt"
        assert spec.fallback_domain == "gdelt.org"
        assert spec.name.startswith("gdelt-")
        # URL must be properly encoded — no raw spaces or parens leak through.
        assert " " not in spec.url
        assert "(" not in spec.url and ")" not in spec.url
        assert "api.gdeltproject.org" in spec.url
        assert "format=json" in spec.url


def test_gdelt_parser_registered() -> None:
    # The pack registers "gdelt" at import time; get_parser must resolve it
    # to our function (not silently fall back to the rss parser).
    parser = get_parser("gdelt")
    assert parser is _parse_gdelt


def test_feeds_appear_in_assemble_feeds() -> None:
    # assemble_feeds() imports catchem.news_sources, which auto-discovers and
    # imports this pack, firing its registration. No network involved.
    feeds = assemble_feeds()
    gdelt_feeds = [f for f in feeds if f.parser == "gdelt"]
    assert gdelt_feeds, "expected at least one gdelt-parser feed in assemble_feeds()"
    assert any(f.name.startswith("gdelt-") for f in gdelt_feeds)
    # The named markets feed specifically should be present.
    assert any(f.name == "gdelt-markets" for f in feeds)


def test_gdelt_edge_cases() -> None:
    from catchem.news_sources.gdelt import _parse_gdelt

    # 1. urlparse throws exception & empty hostname
    body1 = json.dumps({"articles": [
        {"url": "http://[::1]abc", "title": "Corrupt Hostname"},
        {"url": "/foo/bar", "title": "Empty Hostname"},
    ]}).encode("utf-8")

    items1 = _parse_gdelt(body1, fallback_domain="gdelt_fallback.org")
    assert len(items1) == 2
    assert items1[0].domain == "gdelt_fallback.org"
    assert items1[1].domain == "gdelt_fallback.org"

    # 2. Title-less article: fallback to using URL as title
    body2 = json.dumps({"articles": [
        {"url": "https://ok.example.com/x", "title": "  "}, # empty/blank
        {"url": "https://ok.example.com/y"}, # missing title key
    ]}).encode("utf-8")
    items2 = _parse_gdelt(body2, fallback_domain="gdelt.org")
    assert len(items2) == 2
    assert items2[0].title == "https://ok.example.com/x"
    assert items2[1].title == "https://ok.example.com/y"


def test_gdelt_timestamp_parsing_optimized() -> None:
    from catchem.news_sources.gdelt import _parse_seendate, _parse_seendate_cached

    # Clear cache first to ensure predictable behavior
    _parse_seendate_cached.cache_clear()

    # Case 1: Standard format (valid)
    t1 = _parse_seendate("20260528T143000Z")
    assert t1 == datetime(2026, 5, 28, 14, 30, 0, tzinfo=UTC)

    # Cache hit
    info_before = _parse_seendate_cached.cache_info()
    t1_cached = _parse_seendate("20260528T143000Z")
    info_after = _parse_seendate_cached.cache_info()
    assert t1 == t1_cached
    assert info_after.hits == info_before.hits + 1

    # Case 2: Standard format length/structure but invalid numbers
    # Should fail int conversion or datetime bounds, fallback to strptime (which also fails), and return now(UTC)
    t2 = _parse_seendate("20269928T143000Z")
    assert isinstance(t2, datetime)
    assert t2.tzinfo == UTC

    # Case 3: Non-standard format (different length/structure)
    t3 = _parse_seendate("not-a-real-date")
    assert isinstance(t3, datetime)

    # Case 4: Non-string input should bypass cache and return now(UTC)
    t4 = _parse_seendate(123456789)
    assert isinstance(t4, datetime)


