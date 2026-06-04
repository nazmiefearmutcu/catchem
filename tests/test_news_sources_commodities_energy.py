"""Tests for the COMMODITIES / ENERGY / RATES specialist source pack.

NO live network: every assertion runs against the in-process FeedSpec tuple
the provider returns, plus an ``assemble_feeds()`` call that only triggers
in-process auto-discovery + registration (no HTTP is performed).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.commodities_energy import (
    _COMMODITIES_ENERGY_FEEDS,
    commodities_energy_feeds,
)


def test_provider_returns_at_least_eight_feeds() -> None:
    feeds = commodities_energy_feeds()
    assert len(feeds) >= 8
    assert all(isinstance(spec, FeedSpec) for spec in feeds)


def test_feed_names_are_unique_and_namespaced() -> None:
    feeds = commodities_energy_feeds()
    names = [spec.name for spec in feeds]
    assert len(names) == len(set(names)), "duplicate feed names within the pack"
    assert all(name.startswith("ce-") for name in names)


def test_all_urls_are_https() -> None:
    for spec in commodities_energy_feeds():
        parts = urlsplit(spec.url)
        assert parts.scheme == "https", f"{spec.name} is not https: {spec.url}"
        assert parts.netloc, f"{spec.name} has no host: {spec.url}"


def test_every_feed_uses_rss_parser_and_has_fallback_domain() -> None:
    for spec in commodities_energy_feeds():
        # No new parser is introduced by this pack — the default "rss" parser
        # handles every endpoint.
        assert spec.parser == "rss", f"{spec.name} must use the rss parser"
        assert spec.fallback_domain, f"{spec.name} is missing a fallback_domain"
        # The fallback domain should be a bare brand host (no scheme/path).
        assert "/" not in spec.fallback_domain
        assert "://" not in spec.fallback_domain


def test_no_name_collision_with_default_feeds() -> None:
    default_names = {spec.name for spec in DEFAULT_FEEDS}
    pack_names = {spec.name for spec in commodities_energy_feeds()}
    assert default_names.isdisjoint(pack_names), (
        f"commodities/energy pack reuses DEFAULT_FEEDS names: "
        f"{default_names & pack_names}"
    )


def test_feeds_present_in_assemble_feeds() -> None:
    # assemble_feeds() imports catchem.news_sources, which auto-discovers and
    # imports this pack, firing its registration. No network involved.
    assembled = assemble_feeds()
    assembled_names = {spec.name for spec in assembled}
    for spec in commodities_energy_feeds():
        assert spec.name in assembled_names, (
            f"{spec.name} missing from assemble_feeds()"
        )
    # At least eight ce-* feeds must survive the de-dup merge.
    ce_in_assembled = [s for s in assembled if s.name.startswith("ce-")]
    assert len(ce_in_assembled) >= 8


def test_source_tuple_matches_provider_output() -> None:
    # Guard against a slug/url drifting between the data tuple and the public
    # provider (the provider is a thin map over _COMMODITIES_ENERGY_FEEDS).
    assert len(commodities_energy_feeds()) == len(_COMMODITIES_ENERGY_FEEDS)
    expected_names = {f"ce-{slug}" for slug, _url, _dom in _COMMODITIES_ENERGY_FEEDS}
    assert {spec.name for spec in commodities_energy_feeds()} == expected_names


def test_assembled_feed_names_are_globally_unique() -> None:
    # The merged feed set (DEFAULT_FEEDS + every pack) must have no duplicate
    # names after this pack is added — assemble_feeds() de-dups by name.
    names = [spec.name for spec in assemble_feeds()]
    assert len(names) == len(set(names))
