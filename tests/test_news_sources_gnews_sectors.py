"""Contract tests for the Google News SECTOR/THEME query pack.

Pure-offline: we only inspect the FeedSpecs the provider yields and confirm
they merge into ``assemble_feeds()``. No network is touched — the provider is
a deterministic list builder.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from catchem.news_poller import DEFAULT_FEEDS, assemble_feeds
from catchem.news_sources.gnews_sectors import gnews_sector_feeds

# The pack ships roughly twenty sector/theme queries; pin a tolerant band so
# small future additions don't break the suite while still catching a gross
# regression (e.g. an empty provider).
_MIN_FEEDS = 18
_MAX_FEEDS = 24


def test_provider_returns_about_twenty_feeds() -> None:
    feeds = gnews_sector_feeds()
    assert _MIN_FEEDS <= len(feeds) <= _MAX_FEEDS, f"got {len(feeds)} feeds"


def test_all_names_unique_and_prefixed() -> None:
    feeds = gnews_sector_feeds()
    names = [f.name for f in feeds]
    assert len(names) == len(set(names)), "duplicate feed names within the pack"
    assert all(n.startswith("gnews-") for n in names), names


def test_all_urls_are_valid_encoded_google_news_search() -> None:
    for spec in gnews_sector_feeds():
        parts = urlsplit(spec.url)
        assert parts.scheme == "https", spec.url
        assert parts.netloc == "news.google.com", spec.url
        assert parts.path == "/rss/search", spec.url
        qs = parse_qs(parts.query)
        # A non-empty, present `q` query param + the pinned US-English locale.
        assert qs.get("q") and qs["q"][0], spec.url
        assert qs.get("hl") == ["en-US"], spec.url
        assert qs.get("gl") == ["US"], spec.url
        assert qs.get("ceid") == ["US:en"], spec.url
        # Raw spaces must have been URL-encoded (quote_plus → '+', so the
        # encoded query never contains a literal space character).
        assert " " not in parts.query, spec.url
        # Default parser — no custom body parser needed for RSS.
        assert spec.parser == "rss", spec.name
        assert spec.fallback_domain == "news.google.com", spec.name


def test_pack_names_do_not_collide_with_default_feeds() -> None:
    default_names = {f.name for f in DEFAULT_FEEDS}
    pack_names = {f.name for f in gnews_sector_feeds()}
    overlap = default_names & pack_names
    assert not overlap, f"pack reuses DEFAULT_FEEDS names: {sorted(overlap)}"


def test_feeds_appear_in_assemble_feeds() -> None:
    assembled = {f.name: f for f in assemble_feeds()}
    for spec in gnews_sector_feeds():
        assert spec.name in assembled, f"{spec.name} missing from assemble_feeds()"
        # The assembled spec must be the same URL the provider emitted.
        assert assembled[spec.name].url == spec.url, spec.name
