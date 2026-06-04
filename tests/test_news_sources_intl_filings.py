"""Contract tests for the INTERNATIONAL EXCHANGE-FILINGS source pack.

Validate the static FeedSpec shape the ``intl_filings`` pack registers
WITHOUT any network — we never fetch a feed, only assert the declared spec
contract and that the pack is wired into ``assemble_feeds()`` with no name
collisions against ``DEFAULT_FEEDS`` or any other source pack. Keeping it
offline means the suite stays deterministic and fast regardless of publisher
uptime.

Covered:
  * at least 8 feeds contributed,
  * every name unique within the pack AND prefixed ``filing-``,
  * every URL is https with a real host,
  * every feed uses the built-in ``rss`` parser (no new parser registered),
  * a sensible bare-host fallback_domain on each,
  * every pack feed shows up in ``assemble_feeds()`` (auto-discovery wired),
  * zero name collision with ``DEFAULT_FEEDS`` (esp. NO US SEC dup), and
  * zero name collision with every OTHER discovered pack.
"""

from __future__ import annotations

from urllib.parse import urlparse

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.intl_filings import _intl_filings_feeds

# US SEC names already shipping in DEFAULT_FEEDS — this pack must NOT re-declare
# any of them (the task explicitly forbids duplicating US SEC coverage).
_SEC_NAMES = {"sec-edgar-current", "sec-8k", "sec-10q"}


def _specs() -> list[FeedSpec]:
    feeds = list(_intl_filings_feeds())
    assert all(isinstance(f, FeedSpec) for f in feeds)
    return feeds


def test_provider_returns_at_least_eight_feeds() -> None:
    assert len(_specs()) >= 8


def test_names_are_unique() -> None:
    names = [s.name for s in _specs()]
    assert len(names) == len(set(names)), f"duplicate names within pack: {names}"


def test_names_use_filing_prefix() -> None:
    for spec in _specs():
        assert spec.name.startswith("filing-"), f"{spec.name!r} missing 'filing-' prefix"


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
        assert isinstance(fb, str) and fb.strip(), f"{spec.name} empty fallback_domain"
        assert "." in fb, f"{spec.name} fallback_domain looks non-domain: {fb!r}"
        assert "://" not in fb, f"{spec.name} fallback_domain has a scheme: {fb!r}"
        assert "/" not in fb, f"{spec.name} fallback_domain has a path: {fb!r}"
        assert " " not in fb, f"{spec.name} fallback_domain has whitespace: {fb!r}"


def test_fallback_domain_matches_url_host() -> None:
    """Each fallback_domain should plausibly match its feed's host so an item
    arriving without a link attributes to the right publisher."""
    for spec in _specs():
        host = (urlparse(spec.url).hostname or "").lower()
        fb = spec.fallback_domain.lower()
        assert fb in host or host.endswith(fb) or host in fb, (
            f"{spec.name}: fallback_domain {fb!r} unrelated to host {host!r}"
        )


def test_no_collision_with_default_feeds() -> None:
    default_names = {s.name for s in DEFAULT_FEEDS}
    pack_names = {s.name for s in _specs()}
    assert default_names.isdisjoint(pack_names), (
        f"collision with DEFAULT_FEEDS: {default_names & pack_names}"
    )


def test_no_us_sec_duplication() -> None:
    """Explicit guard: the international pack must not re-declare US SEC feeds."""
    pack_names = {s.name for s in _specs()}
    assert pack_names.isdisjoint(_SEC_NAMES)
    # And no pack URL points at sec.gov (would shadow the SEC firehoses).
    for spec in _specs():
        host = (urlparse(spec.url).hostname or "").lower()
        assert not host.endswith("sec.gov"), f"{spec.name} points at sec.gov: {spec.url}"


def test_pack_feeds_present_in_assemble_feeds() -> None:
    assembled = assemble_feeds()
    assembled_names = {s.name for s in assembled}
    for spec in _specs():
        assert spec.name in assembled_names, f"{spec.name} missing from assemble_feeds()"
        # The assembled spec must match the pack's URL (no accidental shadow).
        match = next(a for a in assembled if a.name == spec.name)
        assert match.url == spec.url


def test_assemble_feeds_has_no_duplicate_names_overall() -> None:
    names = [s.name for s in assemble_feeds()]
    assert len(names) == len(set(names)), "assemble_feeds() produced duplicate names"


def test_no_collision_with_other_packs() -> None:
    """The pack's names must be unique across the WHOLE assembled set — i.e.
    no other source pack already claimed a ``filing-`` name. assemble_feeds()
    silently drops a later dup, so an absent feed here would mean a clash."""
    assembled = list(assemble_feeds())
    # Every pack name must resolve to OUR url in the assembled output. If a
    # different pack had registered the same name first, assemble_feeds would
    # keep theirs and this would fail.
    by_name = {s.name: s for s in assembled}
    for spec in _specs():
        assert spec.name in by_name, f"{spec.name} dropped from assemble_feeds()"
        assert by_name[spec.name].url == spec.url, (
            f"{spec.name} shadowed by another pack: "
            f"{by_name[spec.name].url!r} != {spec.url!r}"
        )
