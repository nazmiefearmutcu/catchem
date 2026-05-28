"""Tests for the CRYPTO-DEPTH source pack.

NO live network: every assertion runs against the in-process provider output
and an ``assemble_feeds()`` call that only triggers registration (no HTTP).
The pack adds no parser, so there's nothing to fixture — we just validate the
FeedSpec shape, name uniqueness, https-ness, presence in assemble_feeds(), and
that none of the new names collide with the five default crypto wires.
"""

from __future__ import annotations

from catchem.news_poller import FeedSpec, assemble_feeds
from catchem.news_sources.crypto_depth import _crypto_depth_feeds

# The five crypto feeds already shipped in DEFAULT_FEEDS. The pack must not
# reuse any of these names (assemble_feeds de-dups by name, first-wins).
_EXISTING_CRYPTO_NAMES = frozenset(
    {
        "coindesk-business",
        "decrypt-crypto",
        "theblock",
        "cointelegraph",
        "bitcoinmagazine",
    }
)


def test_provider_returns_at_least_eight_feeds() -> None:
    feeds = _crypto_depth_feeds()
    assert len(feeds) >= 8
    assert all(isinstance(spec, FeedSpec) for spec in feeds)


def test_feed_names_are_unique() -> None:
    feeds = _crypto_depth_feeds()
    names = [spec.name for spec in feeds]
    assert len(names) == len(set(names)), f"duplicate feed names: {names}"


def test_all_urls_are_https() -> None:
    for spec in _crypto_depth_feeds():
        assert spec.url.startswith("https://"), f"{spec.name} is not https: {spec.url}"


def test_every_feed_has_nonempty_fallback_domain_and_rss_parser() -> None:
    for spec in _crypto_depth_feeds():
        assert spec.fallback_domain, f"{spec.name} has no fallback_domain"
        # This pack registers no new parser — everything stays on rss.
        assert spec.parser == "rss", f"{spec.name} unexpectedly uses parser {spec.parser!r}"


def test_no_collision_with_existing_crypto_feed_names() -> None:
    names = {spec.name for spec in _crypto_depth_feeds()}
    overlap = names & _EXISTING_CRYPTO_NAMES
    assert not overlap, f"crypto-depth names collide with defaults: {overlap}"


def test_feeds_appear_in_assemble_feeds() -> None:
    # assemble_feeds() imports catchem.news_sources, which auto-discovers and
    # imports this pack, firing its registration. No network involved.
    assembled = assemble_feeds()
    assembled_names = {spec.name for spec in assembled}

    pack_names = {spec.name for spec in _crypto_depth_feeds()}
    missing = pack_names - assembled_names
    assert not missing, f"crypto-depth feeds missing from assemble_feeds(): {missing}"

    # And the five existing crypto wires are still present (the pack added to,
    # rather than displaced, the default set).
    assert _EXISTING_CRYPTO_NAMES <= assembled_names


def test_assembled_feed_names_remain_unique() -> None:
    # The whole point of assemble_feeds() de-dup: adding this pack must not
    # introduce any duplicate name across the merged set.
    names = [spec.name for spec in assemble_feeds()]
    assert len(names) == len(set(names))
