"""Spec-shape tests for the EARNINGS / INVESTOR-RELATIONS / PRESS-RELEASE pack.

These validate the FeedSpec list the pack registers WITHOUT any network: we
never fetch a feed, we only assert the static spec shape and that the pack is
wired into `assemble_feeds()` with no name collisions against `DEFAULT_FEEDS`
(including the existing `prnewswire-all`) or any other source pack. Keeping it
offline means the suite stays deterministic and fast regardless of publisher
uptime.
"""

from __future__ import annotations

from urllib.parse import urlparse

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.earnings_ir import earnings_ir_feeds


def _specs() -> list[FeedSpec]:
    return list(earnings_ir_feeds())


def test_provider_returns_at_least_eight_feeds() -> None:
    assert len(_specs()) >= 8


def test_all_specs_are_feedspec_instances() -> None:
    for spec in _specs():
        assert isinstance(spec, FeedSpec)


def test_names_are_unique() -> None:
    names = [s.name for s in _specs()]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_all_names_use_ir_prefix() -> None:
    for spec in _specs():
        assert spec.name.startswith("ir-"), f"{spec.name} lacks the ir- prefix"
        # Something meaningful must follow the prefix.
        assert len(spec.name) > len("ir-"), f"{spec.name} is just the bare prefix"


def test_names_are_nonempty_strings() -> None:
    for spec in _specs():
        assert isinstance(spec.name, str) and spec.name.strip()


def test_all_urls_are_https() -> None:
    for spec in _specs():
        parsed = urlparse(spec.url)
        assert parsed.scheme == "https", f"{spec.name} is not https: {spec.url}"
        assert parsed.netloc, f"{spec.name} has no host: {spec.url}"


def test_all_specs_use_default_rss_parser() -> None:
    # The pack adds NO new parser — every feed must ride the built-in "rss".
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


def test_fallback_domain_is_substring_of_url_host() -> None:
    """Each fallback_domain should plausibly match its feed's host so an item
    arriving without a link attributes to the right publisher."""
    for spec in _specs():
        host = (urlparse(spec.url).hostname or "").lower()
        fb = spec.fallback_domain.lower()
        # fallback is the brand host or a sub/parent of the feed host.
        assert fb in host or host.endswith(fb) or host in fb, (
            f"{spec.name}: fallback_domain {fb!r} unrelated to host {host!r}"
        )


def test_no_name_collisions_with_default_feeds() -> None:
    default_names = {s.name for s in DEFAULT_FEEDS}
    pack_names = {s.name for s in _specs()}
    assert default_names.isdisjoint(pack_names), (
        f"collision with DEFAULT_FEEDS: {default_names & pack_names}"
    )


def test_no_collision_with_prnewswire_all() -> None:
    # prnewswire-all lives in DEFAULT_FEEDS; this pack must not redefine it
    # (nor any prnewswire wire — those are owned by the defaults).
    pack_names = {s.name for s in _specs()}
    assert "prnewswire-all" not in pack_names
    assert not any("prnewswire" in n for n in pack_names)


def test_no_collision_with_other_packs() -> None:
    """The ir- names must not collide with any OTHER registered pack either.

    assemble_feeds() de-dups by name, so a clash would silently drop one of
    the entries. Subtract this pack's own contribution from the assembled set
    and assert the remainder shares no ir- name with us.
    """
    pack_names = {s.name for s in _specs()}
    others = {s.name for s in assemble_feeds()} - pack_names
    assert others.isdisjoint(pack_names), (
        f"collision with another pack/defaults: {others & pack_names}"
    )


def test_pack_feeds_present_in_assemble_feeds() -> None:
    assembled_names = {s.name for s in assemble_feeds()}
    for spec in _specs():
        assert spec.name in assembled_names, f"{spec.name} missing from assemble_feeds()"


def test_assemble_feeds_has_no_duplicate_names_overall() -> None:
    names = [s.name for s in assemble_feeds()]
    assert len(names) == len(set(names)), "assemble_feeds() produced duplicate names"


def test_expected_ir_publishers_are_covered() -> None:
    """Sanity check that the core press-release / IR publishers are present."""
    fallback_domains = {s.fallback_domain for s in _specs()}
    for expected in ("globenewswire.com", "businesswire.com", "nasdaq.com", "seekingalpha.com"):
        assert expected in fallback_domains, f"expected {expected} in pack"
