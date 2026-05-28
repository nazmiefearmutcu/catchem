"""Cross-source near-duplicate TITLE suppression in the news poller.

The poller fetches 187 sources; many carry the SAME story, so without title
dedup the Live Feed floods with near-identical headlines that all pass the
exact-URL guard (each outlet has its own URL). These tests pin:

  * `_normalize_title` — source-suffix stripping, punctuation removal,
    whitespace collapse, and the "too short → bypass" rule.
  * the full `_poll_once` ingest path — two near-identical titles from
    DIFFERENT feeds/URLs collapse to ONE record when the window is open;
    distinct titles both ingest; window==0 disables suppression entirely.
  * the per-tick `last_dupe_titles_skipped` counter.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import catchem.news_poller as np
from catchem.news_poller import (
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
    _normalize_title,
)

# ──────────────────────────────────────────────────────────────────────────────
# _normalize_title
# ──────────────────────────────────────────────────────────────────────────────


def test_normalize_title_strips_dash_source_suffix() -> None:
    # The trailing " - Source" attribution is removed so the SAME story
    # carried by two outlets (each appending its own brand) collapses.
    assert _normalize_title("Apple beats earnings - The Verge") == _normalize_title(
        "Apple beats earnings - CNBC"
    )
    assert _normalize_title("Apple beats earnings - The Verge") == "apple beats earnings"


def test_normalize_title_strips_pipe_source_suffix() -> None:
    assert _normalize_title("Bitcoin tops $90k | CoinDesk") == _normalize_title(
        "Bitcoin tops $90k - Reuters"
    )


def test_normalize_title_removes_punctuation_and_lowercases() -> None:
    # Case + punctuation differences must not defeat dedup.
    assert _normalize_title("Fed HOLDS rates, signals patience!") == _normalize_title(
        "fed holds rates signals patience"
    )
    key = _normalize_title("S&P 500 climbs 1.2%")
    assert "&" not in key and "%" not in key
    # Punctuation is replaced by a space (not deleted) so adjacent tokens
    # don't silently merge into a new word.
    assert key == "s p 500 climbs 1 2"


def test_normalize_title_collapses_whitespace() -> None:
    assert _normalize_title("Markets    rally\n\t   today") == "markets rally today"


def test_normalize_title_only_strips_spaced_source_suffix() -> None:
    # The source-suffix regex requires whitespace around the separator
    # (" - X"), so the spaced " - Bloomberg" tail is stripped while the
    # un-spaced interior hyphen in "Trade-war" is handled by generic
    # punctuation removal (→ a space), not treated as a suffix delimiter.
    assert (
        _normalize_title("Trade-war fears hit stocks - Bloomberg")
        == "trade war fears hit stocks"
    )


def test_normalize_title_short_or_empty_returns_empty_to_skip_dedup() -> None:
    # Below ~5 meaningful chars → "" so the caller never suppresses it.
    assert _normalize_title("") == ""
    assert _normalize_title("   ") == ""
    assert _normalize_title("Live") == ""        # 4 chars
    assert _normalize_title("!!! ...") == ""     # punctuation-only
    # Exactly at/above the floor is a real key.
    assert _normalize_title("Crash") == "crash"  # 5 chars


# ──────────────────────────────────────────────────────────────────────────────
# Full _poll_once ingest path
# ──────────────────────────────────────────────────────────────────────────────


class _StubStorage:
    """Storage stub: no record ever pre-exists, so the title gate is the only
    thing that can suppress an item with a fresh URL."""

    def get_record(self, _cap_id: str) -> None:
        return None


class _StubSupervisor:
    def __init__(self) -> None:
        self.storage = _StubStorage()
        self.processed: list[object] = []

    def process_capture(self, cap: object) -> None:
        self.processed.append(cap)


def _make_settings(tmp_path: Path, window: float):
    class _Paths:
        catchem_output_dir = tmp_path

    class _News:
        dedup_title_window_seconds = window

    class _Settings:
        paths = _Paths()
        news = _News()

    return _Settings()


def _item(title: str, url: str, *, when: datetime) -> ParsedItem:
    return ParsedItem(
        title=title,
        text=f"body for {url}",
        url=url,
        domain="example.com",
        published_ts=when,
    )


def _poller_with_canned_items(
    monkeypatch, tmp_path: Path, *, window: float, results: list[FeedFetchResult]
) -> NewsPoller:
    """Build a poller whose parallel fetch returns `results` verbatim."""
    sup = _StubSupervisor()
    poller = NewsPoller(
        supervisor=sup,  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, window),  # type: ignore[arg-type]
        feeds=[r.spec for r in results],
    )

    async def _fake_fetch(_client, spec):
        return next(r for r in results if r.spec.name == spec.name)

    monkeypatch.setattr(np, "fetch_feed_result", _fake_fetch)
    return poller


def test_near_identical_titles_across_feeds_collapse_when_window_open(
    monkeypatch, tmp_path
) -> None:
    now = datetime.now(UTC)
    # Two DIFFERENT feeds, two DIFFERENT URLs, same story (one appends a
    # source suffix + different punctuation/case — both normalize equal).
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    b = FeedSpec("feed-b", "https://b.example/rss", "b.example")
    results = [
        FeedFetchResult(
            spec=a,
            items=(_item("Apple beats earnings expectations", "https://a.example/1", when=now),),
            status_code=200,
        ),
        FeedFetchResult(
            spec=b,
            items=(_item("Apple beats earnings expectations - CNBC", "https://b.example/9", when=now),),
            status_code=200,
        ),
    ]
    poller = _poller_with_canned_items(monkeypatch, tmp_path, window=21600.0, results=results)

    ingested = asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]

    assert ingested == 1, "only the first occurrence of the story should ingest"
    assert poller.last_dupe_titles_skipped == 1
    assert len(poller._sup.processed) == 1  # type: ignore[attr-defined]


def test_distinct_titles_both_ingest(monkeypatch, tmp_path) -> None:
    now = datetime.now(UTC)
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    b = FeedSpec("feed-b", "https://b.example/rss", "b.example")
    results = [
        FeedFetchResult(
            spec=a,
            items=(_item("Apple beats earnings expectations", "https://a.example/1", when=now),),
            status_code=200,
        ),
        FeedFetchResult(
            spec=b,
            items=(_item("Oil prices slump on demand fears", "https://b.example/2", when=now),),
            status_code=200,
        ),
    ]
    poller = _poller_with_canned_items(monkeypatch, tmp_path, window=21600.0, results=results)

    ingested = asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]

    assert ingested == 2
    assert poller.last_dupe_titles_skipped == 0


def test_window_zero_disables_title_suppression(monkeypatch, tmp_path) -> None:
    now = datetime.now(UTC)
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    b = FeedSpec("feed-b", "https://b.example/rss", "b.example")
    # Identical normalized title, distinct URLs — with window==0 BOTH ingest
    # (preserves pre-feature behavior the default tests may rely on).
    results = [
        FeedFetchResult(
            spec=a,
            items=(_item("Fed holds rates steady", "https://a.example/1", when=now),),
            status_code=200,
        ),
        FeedFetchResult(
            spec=b,
            items=(_item("Fed holds rates steady - Reuters", "https://b.example/2", when=now),),
            status_code=200,
        ),
    ]
    poller = _poller_with_canned_items(monkeypatch, tmp_path, window=0.0, results=results)
    assert poller._dedup_title_window == 0.0

    ingested = asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]

    assert ingested == 2, "window==0 must not suppress anything"
    assert poller.last_dupe_titles_skipped == 0


def test_generic_short_title_is_never_suppressed(monkeypatch, tmp_path) -> None:
    now = datetime.now(UTC)
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    b = FeedSpec("feed-b", "https://b.example/rss", "b.example")
    # Two "Live" items normalize to "" → bypass title-dedup, so both ingest
    # even with the window wide open.
    results = [
        FeedFetchResult(
            spec=a, items=(_item("Live", "https://a.example/1", when=now),), status_code=200
        ),
        FeedFetchResult(
            spec=b, items=(_item("Live", "https://b.example/2", when=now),), status_code=200
        ),
    ]
    poller = _poller_with_canned_items(monkeypatch, tmp_path, window=21600.0, results=results)

    ingested = asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]

    assert ingested == 2
    assert poller.last_dupe_titles_skipped == 0


def test_dedup_counter_starts_zero(tmp_path) -> None:
    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, 21600.0),  # type: ignore[arg-type]
        feeds=[],
    )
    assert poller.last_dupe_titles_skipped == 0
    assert poller._dedup_title_window == 21600.0


def test_window_getattr_fallback_when_setting_missing(tmp_path) -> None:
    """A settings object lacking `news.dedup_title_window_seconds` must not
    crash construction — getattr fallback yields 0.0 (suppression off)."""

    class _Paths:
        catchem_output_dir = tmp_path

    class _News:
        pass  # no dedup_title_window_seconds attribute

    class _Settings:
        paths = _Paths()
        news = _News()

    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_Settings(),  # type: ignore[arg-type]
        feeds=[],
    )
    assert poller._dedup_title_window == 0.0
