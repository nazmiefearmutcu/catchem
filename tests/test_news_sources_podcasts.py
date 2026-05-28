"""Spec-shape tests for the finance/markets PODCAST source pack.

These validate the FeedSpec list the pack registers WITHOUT any network:
we never fetch a feed, we only assert the static spec shape and that the
pack is wired into `assemble_feeds()` with no name collisions against
`DEFAULT_FEEDS`. Offline-only keeps the suite deterministic and fast
regardless of podcast-host uptime.
"""

from __future__ import annotations

from urllib.parse import urlparse

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.podcasts import _podcast_feeds


def _specs() -> list[FeedSpec]:
    return list(_podcast_feeds())


def test_provider_returns_at_least_eight_feeds() -> None:
    assert len(_specs()) >= 8


def test_all_specs_are_feedspec_instances() -> None:
    for spec in _specs():
        assert isinstance(spec, FeedSpec)


def test_names_are_unique() -> None:
    names = [s.name for s in _specs()]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_names_use_pod_prefix() -> None:
    for spec in _specs():
        assert spec.name.startswith("pod-"), f"{spec.name} lacks 'pod-' prefix"


def test_names_are_nonempty_strings() -> None:
    for spec in _specs():
        assert isinstance(spec.name, str) and spec.name.strip()


def test_all_urls_are_https() -> None:
    for spec in _specs():
        parsed = urlparse(spec.url)
        assert parsed.scheme == "https", f"{spec.name} is not https: {spec.url}"
        assert parsed.netloc, f"{spec.name} has no host: {spec.url}"


def test_all_specs_use_default_rss_parser() -> None:
    # Podcast feeds ARE RSS — the pack adds NO new parser; every feed must
    # ride the built-in "rss".
    for spec in _specs():
        assert spec.parser == "rss", f"{spec.name} uses non-rss parser {spec.parser!r}"


def test_fallback_domains_are_sensible() -> None:
    for spec in _specs():
        fb = spec.fallback_domain
        assert isinstance(fb, str) and fb.strip(), f"{spec.name} has empty fallback_domain"
        # A bare host: contains a dot, no scheme, no path, no whitespace.
        assert "." in fb, f"{spec.name} fallback_domain looks non-domain: {fb!r}"
        assert "://" not in fb, f"{spec.name} fallback_domain has a scheme: {fb!r}"
        assert "/" not in fb, f"{spec.name} fallback_domain has a path: {fb!r}"
        assert " " not in fb, f"{spec.name} fallback_domain has whitespace: {fb!r}"


def test_no_name_collisions_with_default_feeds() -> None:
    default_names = {s.name for s in DEFAULT_FEEDS}
    pack_names = {s.name for s in _specs()}
    assert default_names.isdisjoint(pack_names), (
        f"collision with DEFAULT_FEEDS: {default_names & pack_names}"
    )


def test_pack_feeds_present_in_assemble_feeds() -> None:
    assembled_names = {s.name for s in assemble_feeds()}
    for spec in _specs():
        assert spec.name in assembled_names, f"{spec.name} missing from assemble_feeds()"


def test_assemble_feeds_has_no_duplicate_names_overall() -> None:
    names = [s.name for s in assemble_feeds()]
    assert len(names) == len(set(names)), "assemble_feeds() produced duplicate names"


def test_expected_podcasts_are_covered() -> None:
    """Sanity check that flagship daily-markets shows are present."""
    names = {s.name for s in _specs()}
    for expected in (
        "pod-bloomberg-surveillance",
        "pod-planet-money",
        "pod-marketplace",
        "pod-odd-lots",
    ):
        assert expected in names, f"expected {expected} in pack"
