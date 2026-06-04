"""Tests for the GDELT GKG theme-targeted firehose source pack.

NO live network: every assertion runs against an in-memory SAMPLE JSON
fixture fed straight to `_parse_gkg`, plus an `assemble_feeds()` call that
only triggers in-process registration (no HTTP).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from catchem.news_poller import ParsedItem, assemble_feeds, get_parser
from catchem.news_sources.gdelt_gkg import _gdelt_gkg_feeds, _parse_gkg

# A representative DOC 2.0 ArtList envelope (theme queries return the same
# shape as keyword ones):
#   * two well-formed articles (one with an explicit `domain`, one without —
#     so the URL-derived path is exercised),
#   * one article with a malformed seendate (must fall back to now/UTC),
#   * one article with NO url (must be skipped),
#   * one entry that isn't even a dict (must be skipped).
SAMPLE_GKG = {
    "articles": [
        {
            "url": "https://www.example-news.com/markets/sp500-record",
            "title": "S&P 500 Closes at Record High on Earnings Beat",
            "seendate": "20260528T143000Z",
            "domain": "example-news.com",
            "sourcecountry": "United States",
        },
        {
            "url": "https://markets.foobar.co.uk/story/rate-cut-bets",
            "title": "Traders Ramp Up Rate-Cut Bets After CPI Miss",
            "seendate": "20260528T090500Z",
            # no `domain` key → derive from the URL host
            "sourcecountry": "United Kingdom",
        },
        {
            "url": "https://biz.example.org/firm-files-chapter11",
            "title": "Retailer Files for Chapter 11 Bankruptcy",
            "seendate": "not-a-real-date",  # malformed → now(UTC) fallback
            "domain": "biz.example.org",
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

SAMPLE_BYTES = json.dumps(SAMPLE_GKG).encode("utf-8")


def test_parse_yields_expected_items_and_skips_malformed() -> None:
    items = _parse_gkg(SAMPLE_BYTES, fallback_domain="gdelt.org")

    # 3 valid articles; the url-less one and the junk string are skipped.
    assert len(items) == 3
    assert all(isinstance(it, ParsedItem) for it in items)
    urls = {it.url for it in items}
    assert "https://www.example-news.com/markets/sp500-record" in urls
    assert all("Headline With No Link" not in it.title for it in items)


def test_first_item_fields_and_utc_timestamp() -> None:
    items = _parse_gkg(SAMPLE_BYTES, fallback_domain="gdelt.org")
    first = next(it for it in items if it.url.endswith("sp500-record"))

    assert first.title == "S&P 500 Closes at Record High on Earnings Beat"
    # GDELT has no body → title is reused verbatim as text.
    assert first.text == first.title
    # explicit `domain` field is honored, `www.` stripped where present.
    assert first.domain == "example-news.com"
    # seendate "20260528T143000Z" parsed exactly, tz-aware UTC.
    assert first.published_ts.tzinfo is not None
    assert first.published_ts.utcoffset() == timedelta(0)
    assert first.published_ts == datetime(2026, 5, 28, 14, 30, 0, tzinfo=UTC)


def test_domain_derived_from_url_when_absent() -> None:
    items = _parse_gkg(SAMPLE_BYTES, fallback_domain="gdelt.org")
    derived = next(it for it in items if it.url.endswith("rate-cut-bets"))
    # no `domain` key → host parsed from URL (no `www.` here).
    assert derived.domain == "markets.foobar.co.uk"


def test_malformed_seendate_falls_back_to_now_utc() -> None:
    before = datetime.now(UTC)
    items = _parse_gkg(SAMPLE_BYTES, fallback_domain="gdelt.org")
    after = datetime.now(UTC)
    bankrupt = next(it for it in items if it.url.endswith("firm-files-chapter11"))

    assert bankrupt.published_ts.tzinfo is not None
    # Fallback timestamp is "now", i.e. within the call window.
    assert before <= bankrupt.published_ts <= after


def test_every_item_has_utc_published_ts() -> None:
    items = _parse_gkg(SAMPLE_BYTES, fallback_domain="gdelt.org")
    for it in items:
        assert it.published_ts.tzinfo is not None
        assert it.published_ts.utcoffset() == timedelta(0)


def test_invalid_json_returns_empty_list() -> None:
    assert _parse_gkg(b"not json at all{", fallback_domain="gdelt.org") == []
    assert _parse_gkg(b"", fallback_domain="gdelt.org") == []
    # valid JSON but wrong shape (no "articles" list) → []
    assert _parse_gkg(b'{"status":"ok"}', fallback_domain="gdelt.org") == []
    assert _parse_gkg(b"[1, 2, 3]", fallback_domain="gdelt.org") == []


def test_url_less_articles_are_skipped() -> None:
    body = json.dumps({"articles": [
        {"title": "no url here", "seendate": "20260528T120000Z"},
        {"url": "   ", "title": "blank url"},
        {"url": "https://ok.example.com/x", "title": "good one"},
    ]}).encode("utf-8")
    items = _parse_gkg(body, fallback_domain="gdelt.org")
    assert len(items) == 1
    assert items[0].url == "https://ok.example.com/x"


def test_provider_returns_gkg_feedspecs() -> None:
    feeds = _gdelt_gkg_feeds()
    assert len(feeds) >= 6
    for spec in feeds:
        assert spec.parser == "gdelt_gkg"
        assert spec.fallback_domain == "gdelt.org"
        assert spec.name.startswith("gdelt-gkg-")
        # URL must be properly encoded — no raw spaces or colons leak through.
        assert " " not in spec.url
        # the theme: colon must be percent-encoded (%3A) by quote_plus
        assert "theme%3A" in spec.url
        assert "api.gdeltproject.org" in spec.url
        assert "mode=ArtList" in spec.url
        assert "format=json" in spec.url


def test_gkg_parser_registered() -> None:
    # The pack registers "gdelt_gkg" at import time; get_parser must resolve it
    # to our function (not silently fall back to the rss parser).
    parser = get_parser("gdelt_gkg")
    assert parser is _parse_gkg


def test_feeds_appear_in_assemble_feeds_without_colliding() -> None:
    # assemble_feeds() imports catchem.news_sources, which auto-discovers and
    # imports this pack, firing its registration. No network involved.
    feeds = assemble_feeds()
    gkg_feeds = [f for f in feeds if f.parser == "gdelt_gkg"]
    assert gkg_feeds, "expected at least one gdelt_gkg-parser feed in assemble_feeds()"
    assert all(f.name.startswith("gdelt-gkg-") for f in gkg_feeds)
    # A specific theme feed should be present.
    assert any(f.name == "gdelt-gkg-stockmarket" for f in feeds)

    # Names must NOT collide with the base pack's "gdelt-*" (non-gkg) feeds.
    base_gdelt_names = {
        f.name for f in feeds if f.parser == "gdelt"
    }
    gkg_names = {f.name for f in gkg_feeds}
    assert base_gdelt_names.isdisjoint(gkg_names)
    # And every name across the assembled set is unique (no dupes introduced).
    all_names = [f.name for f in feeds]
    assert len(all_names) == len(set(all_names))
