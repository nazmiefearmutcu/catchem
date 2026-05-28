"""Contract tests for the Google News GLOBAL (non-English) locale pack.

Pure-offline: we only inspect the FeedSpecs the provider yields and confirm
they merge into ``assemble_feeds()``. No network is touched — the provider is a
deterministic list builder.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from catchem.news_poller import DEFAULT_FEEDS, assemble_feeds
from catchem.news_sources.gnews_global import gnews_global_feeds

# The pack ships roughly a dozen locale feeds; pin a tolerant lower bound so
# small future additions don't break the suite while still catching a gross
# regression (e.g. an empty provider).
_MIN_FEEDS = 10


def test_provider_returns_at_least_ten_feeds() -> None:
    feeds = gnews_global_feeds()
    assert len(feeds) >= _MIN_FEEDS, f"got {len(feeds)} feeds"


def test_all_names_unique_and_lang_prefixed() -> None:
    feeds = gnews_global_feeds()
    names = [f.name for f in feeds]
    assert len(names) == len(set(names)), "duplicate feed names within the pack"
    assert all(n.startswith("gnews-") for n in names), names


def test_names_do_not_collide_with_existing_gnews_packs() -> None:
    """gnews-<lang> must not reuse any other registered feed's name.

    Other packs use ``gnews-<slug>`` (sectors) and ``gnews-tkr-*`` (tickers);
    the built-in set uses ``gnews-finance`` etc. assemble_feeds() de-dups by
    name keeping the first occurrence, so a collision would silently drop one of
    our locale feeds. Assert every locale name is unique across the FULL
    assembled set minus our own pack.
    """
    pack_names = {f.name for f in gnews_global_feeds()}
    others = [f.name for f in assemble_feeds() if f.name not in pack_names]
    overlap = pack_names & set(others)
    assert not overlap, f"locale names collide with existing feeds: {sorted(overlap)}"


def test_all_urls_are_valid_encoded_google_news_search() -> None:
    for spec in gnews_global_feeds():
        parts = urlsplit(spec.url)
        assert parts.scheme == "https", spec.url
        assert parts.netloc == "news.google.com", spec.url
        assert parts.path == "/rss/search", spec.url
        qs = parse_qs(parts.query)
        # A non-empty, present `q` plus the per-locale hl/gl/ceid triple.
        assert qs.get("q") and qs["q"][0], spec.url
        assert qs.get("hl") and qs["hl"][0], spec.url
        assert qs.get("gl") and qs["gl"][0], spec.url
        ceid = qs.get("ceid")
        assert ceid and ceid[0], spec.url
        # ceid is the "<COUNTRY>:<lang>" edition id.
        assert ":" in ceid[0], spec.url
        # These are non-English locales — none should pin the US-English edition.
        assert qs["hl"][0] != "en-US", spec.url
        # Raw spaces must have been URL-encoded (quote_plus → '+', so the
        # encoded query never contains a literal space character).
        assert " " not in parts.query, spec.url
        # Default parser — no custom body parser needed for RSS.
        assert spec.parser == "rss", spec.name
        assert spec.fallback_domain == "news.google.com", spec.name


def test_feeds_appear_in_assemble_feeds() -> None:
    assembled = {f.name: f for f in assemble_feeds()}
    for spec in gnews_global_feeds():
        assert spec.name in assembled, f"{spec.name} missing from assemble_feeds()"
        # The assembled spec must be the same URL the provider emitted.
        assert assembled[spec.name].url == spec.url, spec.name
