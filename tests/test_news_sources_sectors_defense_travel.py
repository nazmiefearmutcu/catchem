"""Tests for the DEFENSE / AEROSPACE / AIRLINES / TRAVEL sector source pack.

NO live network: every assertion runs against the in-process FeedSpec tuple
the provider returns, plus an ``assemble_feeds()`` call that only triggers
in-process auto-discovery + registration (no HTTP is performed).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.sectors_defense_travel import (
    _DEFENSE_TRAVEL_FEEDS,
    defense_travel_feeds,
)


def test_provider_returns_at_least_eight_feeds() -> None:
    feeds = defense_travel_feeds()
    assert len(feeds) >= 8
    assert all(isinstance(spec, FeedSpec) for spec in feeds)


def test_feed_names_are_unique_and_namespaced() -> None:
    feeds = defense_travel_feeds()
    names = [spec.name for spec in feeds]
    assert len(names) == len(set(names)), "duplicate feed names within the pack"
    assert all(name.startswith("dt-") for name in names)


def test_all_urls_are_https() -> None:
    for spec in defense_travel_feeds():
        parts = urlsplit(spec.url)
        assert parts.scheme == "https", f"{spec.name} is not https: {spec.url}"
        assert parts.netloc, f"{spec.name} has no host: {spec.url}"


def test_every_feed_uses_rss_parser_and_has_fallback_domain() -> None:
    for spec in defense_travel_feeds():
        # No new parser is introduced by this pack — the default "rss" parser
        # handles every endpoint.
        assert spec.parser == "rss", f"{spec.name} must use the rss parser"
        assert spec.fallback_domain, f"{spec.name} is missing a fallback_domain"
        # The fallback domain should be a bare brand host (no scheme/path).
        assert "/" not in spec.fallback_domain
        assert "://" not in spec.fallback_domain


def test_no_name_collision_with_default_feeds() -> None:
    default_names = {spec.name for spec in DEFAULT_FEEDS}
    pack_names = {spec.name for spec in defense_travel_feeds()}
    assert default_names.isdisjoint(pack_names), (
        f"defense/travel pack reuses DEFAULT_FEEDS names: "
        f"{default_names & pack_names}"
    )


def test_no_name_collision_with_other_packs() -> None:
    # Every other pack's feeds reach assemble_feeds() via auto-discovery. The
    # dt-* slice must be exactly this pack's feeds — i.e. no OTHER pack also
    # emits a dt-* name (which would collide and silently drop on de-dup).
    assembled = assemble_feeds()
    dt_assembled = {s.name for s in assembled if s.name.startswith("dt-")}
    pack_names = {spec.name for spec in defense_travel_feeds()}
    assert dt_assembled == pack_names, (
        f"another pack emits dt-* names colliding with this pack: "
        f"{dt_assembled ^ pack_names}"
    )


def test_feeds_present_in_assemble_feeds() -> None:
    # assemble_feeds() imports catchem.news_sources, which auto-discovers and
    # imports this pack, firing its registration. No network involved.
    assembled = assemble_feeds()
    assembled_names = {spec.name for spec in assembled}
    for spec in defense_travel_feeds():
        assert spec.name in assembled_names, (
            f"{spec.name} missing from assemble_feeds()"
        )
    # At least eight dt-* feeds must survive the de-dup merge.
    dt_in_assembled = [s for s in assembled if s.name.startswith("dt-")]
    assert len(dt_in_assembled) >= 8


def test_source_tuple_matches_provider_output() -> None:
    # Guard against a slug/url drifting between the data tuple and the public
    # provider (the provider is a thin map over _DEFENSE_TRAVEL_FEEDS).
    assert len(defense_travel_feeds()) == len(_DEFENSE_TRAVEL_FEEDS)
    expected_names = {f"dt-{slug}" for slug, _url, _dom in _DEFENSE_TRAVEL_FEEDS}
    assert {spec.name for spec in defense_travel_feeds()} == expected_names


def test_assembled_feed_names_are_globally_unique() -> None:
    # The merged feed set (DEFAULT_FEEDS + every pack) must have no duplicate
    # names after this pack is added — assemble_feeds() de-dups by name.
    names = [spec.name for spec in assemble_feeds()]
    assert len(names) == len(set(names))
