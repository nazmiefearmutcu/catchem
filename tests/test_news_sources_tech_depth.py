"""Contract tests for the TECH / BUSINESS-DEPTH source pack.

Pure offline checks — the pack is a static list of RSS FeedSpecs, so we can
fully verify its shape without touching the network:

  * the provider yields a healthy number of feeds (>= 10),
  * names are unique within the pack,
  * every URL is HTTPS and every FeedSpec carries a fallback_domain,
  * the pack uses the built-in "rss" parser (it must register no new parser),
  * every pack feed shows up in ``assemble_feeds()`` (i.e. the
    ``@register_feed_provider`` decorator + auto-discovery actually wire it in),
  * no pack name collides with a DEFAULT_FEEDS name (collisions would be
    silently dropped by assemble_feeds, hiding a feed).
"""

from __future__ import annotations

from urllib.parse import urlparse

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.tech_depth import _tech_depth_feeds


def _pack_feeds() -> list[FeedSpec]:
    return list(_tech_depth_feeds())


def test_pack_returns_at_least_ten_feeds() -> None:
    feeds = _pack_feeds()
    assert len(feeds) >= 10, f"expected >=10 tech-depth feeds, got {len(feeds)}"


def test_pack_names_are_unique() -> None:
    names = [f.name for f in _pack_feeds()]
    assert len(names) == len(set(names)), "duplicate feed name within tech_depth pack"


def test_every_feed_is_https_with_fallback_domain() -> None:
    for feed in _pack_feeds():
        scheme = urlparse(feed.url).scheme
        assert scheme == "https", f"{feed.name} url is not https: {feed.url!r}"
        assert feed.fallback_domain, f"{feed.name} is missing a fallback_domain"


def test_pack_uses_builtin_rss_parser_only() -> None:
    # Pure-RSS pack: it must NOT introduce a new parser key.
    for feed in _pack_feeds():
        assert feed.parser == "rss", f"{feed.name} should use the built-in rss parser"


def test_pack_feeds_present_in_assemble_feeds() -> None:
    assembled = {f.name: f for f in assemble_feeds()}
    for feed in _pack_feeds():
        assert feed.name in assembled, f"{feed.name} missing from assemble_feeds()"
        # The merged spec must be the pack's own (same URL), proving the
        # provider — not a same-named default — supplied it.
        assert assembled[feed.name].url == feed.url


def test_no_name_collision_with_default_feeds() -> None:
    default_names = {f.name for f in DEFAULT_FEEDS}
    pack_names = {f.name for f in _pack_feeds()}
    overlap = default_names & pack_names
    assert not overlap, f"tech_depth names collide with DEFAULT_FEEDS: {sorted(overlap)}"
