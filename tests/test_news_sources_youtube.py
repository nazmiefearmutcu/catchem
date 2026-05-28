"""Contract tests for the YouTube finance/markets source pack.

Static-only (no live network — the pack merely declares channel Atom URLs):
  * at least 8 feeds contributed,
  * every name unique within the pack and prefixed ``yt-``,
  * every URL is a valid youtube.com per-channel Atom feed
    (``https://www.youtube.com/feeds/videos.xml?channel_id=<UCID>``),
  * every feed uses the built-in ``rss`` parser (Atom rides the default path),
  * fallback_domain is youtube.com,
  * every pack feed shows up in ``assemble_feeds()`` (auto-discovery wired),
  * zero name collision with ``DEFAULT_FEEDS``, and assembled names stay unique.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources import youtube


def _pack_feeds() -> list[FeedSpec]:
    feeds = list(youtube._youtube_feeds())
    assert all(isinstance(f, FeedSpec) for f in feeds)
    return feeds


def test_pack_contributes_at_least_eight_feeds() -> None:
    assert len(_pack_feeds()) >= 8


def test_pack_feed_names_are_unique_and_prefixed() -> None:
    names = [f.name for f in _pack_feeds()]
    assert len(names) == len(set(names)), "duplicate feed name within pack"
    for name in names:
        assert name.startswith("yt-"), f"{name} must be prefixed yt-"


def test_pack_urls_are_valid_youtube_channel_atom() -> None:
    for f in _pack_feeds():
        parts = urlsplit(f.url)
        assert parts.scheme == "https", f"{f.name} is not https: {f.url}"
        assert parts.netloc == "www.youtube.com", f"{f.name} wrong host: {f.url}"
        assert parts.path == "/feeds/videos.xml", f"{f.name} wrong path: {f.url}"
        qs = parse_qs(parts.query)
        cids = qs.get("channel_id", [])
        assert len(cids) == 1, f"{f.name} must carry exactly one channel_id"
        cid = cids[0]
        assert cid.startswith("UC") and len(cid) == 24, (
            f"{f.name} channel_id not a UC… id: {cid!r}"
        )


def test_pack_uses_builtin_rss_parser() -> None:
    """Atom feeds ride the default rss parser — no new parser registered."""
    for f in _pack_feeds():
        assert f.parser == "rss", f"{f.name} must use the rss parser"


def test_pack_sets_youtube_fallback_domain() -> None:
    for f in _pack_feeds():
        assert f.fallback_domain == "youtube.com", f"{f.name} wrong fallback_domain"


def test_no_collision_with_default_feeds() -> None:
    default_names = {f.name for f in DEFAULT_FEEDS}
    pack_names = {f.name for f in _pack_feeds()}
    assert pack_names.isdisjoint(default_names), (
        f"pack reuses DEFAULT_FEEDS names: {pack_names & default_names}"
    )


def test_pack_feeds_present_in_assembled_feeds() -> None:
    """Auto-discovery must surface every pack feed through assemble_feeds()."""
    assembled = assemble_feeds()
    assembled_names = {f.name for f in assembled}
    for f in _pack_feeds():
        assert f.name in assembled_names, f"{f.name} missing from assemble_feeds()"
        match = next(a for a in assembled if a.name == f.name)
        assert match.url == f.url


def test_assembled_feed_names_remain_unique() -> None:
    """Merging the pack must not introduce duplicate names overall."""
    names = [f.name for f in assemble_feeds()]
    assert len(names) == len(set(names))
