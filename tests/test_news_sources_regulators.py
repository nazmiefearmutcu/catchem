"""Contract tests for the central-banks + regulators source pack.

Covers the static FeedSpec contract the ``regulators`` pack must satisfy:
  * at least 8 feeds contributed,
  * every name unique within the pack,
  * every URL is https,
  * every pack feed shows up in ``assemble_feeds()`` (auto-discovery wired),
  * zero name collision with ``DEFAULT_FEEDS`` (the five Fed/SEC/ECB names
    that already ship must not be duplicated), and
  * every feed uses the built-in ``rss`` parser (no new parser registered).

NO live network: the pack only declares URLs; nothing here fetches them.
"""

from __future__ import annotations

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources import regulators

# Names that already exist in DEFAULT_FEEDS for this domain — the pack must
# NOT re-declare any of these (assemble_feeds keeps the DEFAULT_FEEDS one and
# would silently drop a pack dup, so we assert disjointness explicitly).
_PREEXISTING = {"fed-press-all", "sec-edgar-current", "sec-8k", "sec-10q", "ecb-press"}


def _pack_feeds() -> list[FeedSpec]:
    feeds = list(regulators._regulator_feeds())
    assert all(isinstance(f, FeedSpec) for f in feeds)
    return feeds


def test_pack_contributes_at_least_eight_feeds() -> None:
    assert len(_pack_feeds()) >= 8


def test_pack_feed_names_are_unique() -> None:
    names = [f.name for f in _pack_feeds()]
    assert len(names) == len(set(names)), "duplicate feed name within pack"


def test_pack_urls_are_https() -> None:
    for f in _pack_feeds():
        assert f.url.startswith("https://"), f"{f.name} is not https: {f.url}"


def test_pack_uses_builtin_rss_parser() -> None:
    """No new parser — every regulator feed must ride the default rss path."""
    for f in _pack_feeds():
        assert f.parser == "rss", f"{f.name} must use the rss parser"


def test_pack_sets_fallback_domain() -> None:
    for f in _pack_feeds():
        assert f.fallback_domain, f"{f.name} missing fallback_domain"


def test_no_collision_with_default_feeds() -> None:
    default_names = {f.name for f in DEFAULT_FEEDS}
    pack_names = {f.name for f in _pack_feeds()}
    assert pack_names.isdisjoint(default_names), (
        f"pack reuses DEFAULT_FEEDS names: {pack_names & default_names}"
    )
    # Belt-and-suspenders: explicitly assert the known pre-existing names
    # are not re-declared by this pack.
    assert pack_names.isdisjoint(_PREEXISTING)


def test_pack_feeds_present_in_assembled_feeds() -> None:
    """Auto-discovery must surface every pack feed through assemble_feeds()."""
    assembled = assemble_feeds()
    assembled_names = {f.name for f in assembled}
    for f in _pack_feeds():
        assert f.name in assembled_names, f"{f.name} missing from assemble_feeds()"
        # The assembled spec must match the pack's URL (no accidental shadow).
        match = next(a for a in assembled if a.name == f.name)
        assert match.url == f.url


def test_assembled_feed_names_remain_unique() -> None:
    """Merging the pack must not introduce duplicate names overall."""
    names = [f.name for f in assemble_feeds()]
    assert len(names) == len(set(names))
