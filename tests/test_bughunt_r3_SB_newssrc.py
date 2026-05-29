"""Round-3 bug-hunt regression tests for the SB-newssrc file group.

Covers two confirmed findings:

1. GDELT parser never HTML-unescapes titles (gdelt.py + gdelt_gkg.py via the
   shared `_parse_gdelt`). GDELT lifts the raw page <title> verbatim into the
   JSON string, so entities like "&amp;"/"&#39;" survive undecoded — corrupting
   display text AND defeating the poller's cross-source title-dedup (the GDELT
   copy of a story would never collapse against the RSS copy, which the XML
   parser already decoded).

2. HN Algolia parser fetched over plaintext http:// instead of https://.

NO live network: parser assertions run against in-memory fixtures; URL/scheme
assertions read the assembled FeedSpec URLs (no HTTP).
"""

from __future__ import annotations

import json

from catchem.news_poller import _normalize_title
from catchem.news_sources.gdelt import _parse_gdelt
from catchem.news_sources.gdelt_gkg import _parse_gkg
from catchem.news_sources.hn_algolia import _build_url as _hn_build_url
from catchem.news_sources.hn_algolia import _hn_algolia_feeds


# ---------------------------------------------------------------------------
# Finding 1: GDELT title HTML-unescape
# ---------------------------------------------------------------------------

# Titles exactly as GDELT delivers them: raw HTML entities embedded in the JSON
# string value. The expected (decoded) forms are what a clean RSS wire yields.
_AMP_RAW = "Apple &amp; Microsoft beat earnings"
_AMP_CLEAN = "Apple & Microsoft beat earnings"
_APOS_RAW = "Tesla&#39;s margins under pressure"
_APOS_CLEAN = "Tesla's margins under pressure"

_GDELT_ENTITY_BODY = json.dumps({
    "articles": [
        {
            "url": "https://example-news.com/markets/apple-msft",
            "title": _AMP_RAW,
            "seendate": "20260528T143000Z",
            "domain": "example-news.com",
        },
        {
            "url": "https://example-news.com/markets/tesla-margins",
            "title": _APOS_RAW,
            "seendate": "20260528T090500Z",
            "domain": "example-news.com",
        },
    ]
}).encode("utf-8")


def test_gdelt_title_html_unescaped_for_display() -> None:
    """Display text must be the decoded headline, not raw entities."""
    items = _parse_gdelt(_GDELT_ENTITY_BODY, fallback_domain="gdelt.org")
    titles = {it.title for it in items}

    assert _AMP_CLEAN in titles
    assert _APOS_CLEAN in titles
    # Raw entities must NOT survive into display text.
    assert "&amp;" not in " ".join(titles)
    assert "&#39;" not in " ".join(titles)
    # text mirrors the (now-clean) title for body-less GDELT rows.
    for it in items:
        assert it.text == it.title


def test_gdelt_unescaped_title_matches_rss_dedup_key() -> None:
    """The dedup key for the GDELT copy must equal the RSS (clean) copy's key.

    Pre-fix the raw "&amp;" yielded a spurious 'amp' token and "&#39;" a
    spurious '39' token, so the GDELT item never collapsed against the RSS copy
    of the same story. After unescaping, the normalized keys match exactly.
    """
    items = _parse_gdelt(_GDELT_ENTITY_BODY, fallback_domain="gdelt.org")
    by_url = {it.url: it for it in items}

    gdelt_amp_key = _normalize_title(by_url["https://example-news.com/markets/apple-msft"].title)
    gdelt_apos_key = _normalize_title(by_url["https://example-news.com/markets/tesla-margins"].title)

    # An RSS wire delivers the already-decoded headline.
    assert gdelt_amp_key == _normalize_title(_AMP_CLEAN)
    assert gdelt_apos_key == _normalize_title(_APOS_CLEAN)
    # And the dedup-defeating tokens are gone.
    assert "amp" not in gdelt_amp_key.split()
    assert "39" not in gdelt_apos_key.split()


def test_gdelt_gkg_inherits_unescape() -> None:
    """gdelt_gkg reuses _parse_gdelt verbatim, so it inherits the fix."""
    items = _parse_gkg(_GDELT_ENTITY_BODY, fallback_domain="gdelt.org")
    titles = {it.title for it in items}
    assert _AMP_CLEAN in titles
    assert _APOS_CLEAN in titles
    assert "&amp;" not in " ".join(titles)


# ---------------------------------------------------------------------------
# Finding 2: HN Algolia must use https://
# ---------------------------------------------------------------------------

def test_hn_algolia_url_uses_https() -> None:
    url = _hn_build_url("stocks")
    assert url.startswith("https://hn.algolia.com/api/v1/search_by_date")
    assert not url.startswith("http://")


def test_hn_algolia_feedspecs_all_https() -> None:
    feeds = _hn_algolia_feeds()
    assert feeds, "expected at least one hn-algolia feed"
    for spec in feeds:
        assert spec.url.startswith("https://"), spec.url
        assert "http://hn.algolia.com" not in spec.url
