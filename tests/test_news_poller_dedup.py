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


def _make_settings(tmp_path: Path, window: float, *, adaptive: bool = True):
    class _Paths:
        catchem_output_dir = tmp_path

    class _News:
        dedup_title_window_seconds = window
        # Explicit so rollback tests can disable the emptiness ladder and keep
        # every feed "due" on every cycle. Default True == the getattr fallback
        # the poller already applies, so existing callers are unaffected.
        adaptive_polling_enabled = adaptive

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


# ──────────────────────────────────────────────────────────────────────────────
# Rollback-on-ingest-failure (the title-dedup gate is side-effecting: it records
# "seen" during the phase-1 filter, but the ACTUAL ingest happens later and can
# raise. Without a rollback the headline stays suppressed for the whole window
# even though no copy ever landed, so the story is lost from the feed until the
# window expires. These pin `_rollback_title` firing on every failed-ingest path
# while leaving the dedup intact for items that DID ingest.)
# ──────────────────────────────────────────────────────────────────────────────


class _FlakySupervisor:
    """Supervisor stub with switchable ingest failure.

    Two independent controls (use whichever a test needs):
      * ``fail`` flag        — when True, EVERY ``process_capture`` raises.
      * ``fail_calls`` count — raise for the first N calls, then succeed
        (deterministic because both ingest paths process items in order).

    Captures that DON'T raise are appended to ``processed``.
    """

    def __init__(self) -> None:
        self.storage = _StubStorage()
        self.processed: list[object] = []
        self.fail = False
        self.fail_calls = 0

    def process_capture(self, cap: object) -> None:
        if self.fail:
            raise RuntimeError("simulated ingest failure (flag)")
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise RuntimeError("simulated ingest failure (countdown)")
        self.processed.append(cap)


def _mutable_fetcher(monkeypatch):
    """Install a fetch stub backed by a mutable {feed_name: result} map, so a
    test can drive several `_poll_once` cycles with different canned items."""
    holder: dict[str, dict[str, FeedFetchResult]] = {"mapping": {}}

    async def _fake_fetch(_client, spec):
        return holder["mapping"][spec.name]

    monkeypatch.setattr(np, "fetch_feed_result", _fake_fetch)
    return holder


def test_failed_ingest_rolls_back_title_so_a_later_cycle_reingests(
    monkeypatch, tmp_path
) -> None:
    # Cycle 1: feed-a carries the story but its ingest RAISES → the title must
    # be rolled back out of `_seen_titles`. Cycle 2: a *different* URL (another
    # outlet) carries the SAME headline and now ingests cleanly — only possible
    # because the failed first sighting was rolled back. Without the fix the
    # headline would stay "seen" for the full 6h window and never land.
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    b = FeedSpec("feed-b", "https://b.example/rss", "b.example")
    sup = _FlakySupervisor()
    poller = NewsPoller(
        supervisor=sup,  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, 21600.0, adaptive=False),  # type: ignore[arg-type]
        feeds=[a, b],
    )
    holder = _mutable_fetcher(monkeypatch)
    title = "Global markets tumble on surprise rate decision"
    key = _normalize_title(title)

    # ── Cycle 1: ingest fails ──────────────────────────────────────────────
    now = datetime.now(UTC)
    holder["mapping"] = {
        "feed-a": FeedFetchResult(
            spec=a, items=(_item(title, "https://a.example/1", when=now),), status_code=200
        ),
        "feed-b": FeedFetchResult(spec=b, items=(), status_code=200),
    }
    sup.fail = True
    ingested1 = asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert ingested1 == 0
    assert sup.processed == []
    assert key not in poller._seen_titles, "failed ingest must roll the title back out"

    # ── Cycle 2: a new outlet re-serves the same headline, ingest succeeds ──
    later = now
    holder["mapping"] = {
        "feed-a": FeedFetchResult(spec=a, items=(), status_code=200),
        "feed-b": FeedFetchResult(
            spec=b,
            items=(_item(f"{title} - Reuters", "https://b.example/2", when=later),),
            status_code=200,
        ),
    }
    sup.fail = False
    ingested2 = asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert ingested2 == 1, "rolled-back headline must get a fresh chance to ingest"
    assert len(sup.processed) == 1
    assert key in poller._seen_titles, "successful ingest re-records the title"


def test_successful_ingest_keeps_title_recorded_across_cycles(
    monkeypatch, tmp_path
) -> None:
    # Negative control: rollback must be SURGICAL — when the ingest succeeds the
    # title stays recorded, so a later outlet's duplicate is still suppressed.
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    b = FeedSpec("feed-b", "https://b.example/rss", "b.example")
    sup = _FlakySupervisor()  # never fails
    poller = NewsPoller(
        supervisor=sup,  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, 21600.0, adaptive=False),  # type: ignore[arg-type]
        feeds=[a, b],
    )
    holder = _mutable_fetcher(monkeypatch)
    title = "Central bank signals an extended pause on hikes"
    now = datetime.now(UTC)

    holder["mapping"] = {
        "feed-a": FeedFetchResult(
            spec=a, items=(_item(title, "https://a.example/1", when=now),), status_code=200
        ),
        "feed-b": FeedFetchResult(spec=b, items=(), status_code=200),
    }
    assert asyncio.run(poller._poll_once(client=None)) == 1  # type: ignore[arg-type]
    assert len(sup.processed) == 1

    # Second outlet, same headline, fresh URL → must be suppressed (the prior
    # successful sighting is still inside the window).
    holder["mapping"] = {
        "feed-a": FeedFetchResult(spec=a, items=(), status_code=200),
        "feed-b": FeedFetchResult(
            spec=b,
            items=(_item(f"{title} | Bloomberg", "https://b.example/2", when=now),),
            status_code=200,
        ),
    }
    assert asyncio.run(poller._poll_once(client=None)) == 0  # type: ignore[arg-type]
    assert poller.last_dupe_titles_skipped == 1
    assert len(sup.processed) == 1, "no second ingest — dedup record survived"


def test_probe_failed_ingest_rolls_back_and_does_not_inflate_counter(
    monkeypatch, tmp_path
) -> None:
    # The manual-probe path had the SAME side-effecting-gate bug AND a dishonest
    # counter (`total_ingested += len(new_items)` counted swallowed failures as
    # successes). A failed probe ingest must: roll the title back, and leave
    # total_ingested at 0.
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    sup = _FlakySupervisor()
    poller = NewsPoller(
        supervisor=sup,  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, 21600.0),  # type: ignore[arg-type]
        feeds=[a],
    )
    title = "Treasury yields spike after hot inflation print"
    key = _normalize_title(title)
    now = datetime.now(UTC)

    async def _fake_fetch(_client, _spec):
        return FeedFetchResult(
            spec=a, items=(_item(title, "https://a.example/1", when=now),), status_code=200
        )

    monkeypatch.setattr(np, "fetch_feed_result", _fake_fetch)
    sup.fail = True

    asyncio.run(poller.probe_feed_async("https://a.example/rss"))

    assert sup.processed == []
    assert poller.total_ingested == 0, "a swallowed ingest failure must not inflate the counter"
    assert key not in poller._seen_titles, "failed probe ingest must roll the title back"


def test_probe_counts_and_records_only_successful_ingests(monkeypatch, tmp_path) -> None:
    # Probe fetches two DISTINCT-title items; the FIRST ingest fails, the second
    # succeeds. total_ingested must reflect ONLY the success (1, not 2); the
    # failed title is rolled back while the successful one stays recorded.
    a = FeedSpec("feed-a", "https://a.example/rss", "a.example")
    sup = _FlakySupervisor()
    poller = NewsPoller(
        supervisor=sup,  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, 21600.0),  # type: ignore[arg-type]
        feeds=[a],
    )
    bad_title = "Chipmaker warns on weak data-center demand"
    good_title = "Airline lifts full-year profit outlook on travel boom"
    now = datetime.now(UTC)

    async def _fake_fetch(_client, _spec):
        return FeedFetchResult(
            spec=a,
            items=(
                _item(bad_title, "https://a.example/1", when=now),
                _item(good_title, "https://a.example/2", when=now),
            ),
            status_code=200,
        )

    monkeypatch.setattr(np, "fetch_feed_result", _fake_fetch)
    sup.fail_calls = 1  # only the first process_capture raises

    asyncio.run(poller.probe_feed_async("https://a.example/rss"))

    assert len(sup.processed) == 1
    assert poller.total_ingested == 1, "counter must exclude the failed ingest"
    assert _normalize_title(bad_title) not in poller._seen_titles
    assert _normalize_title(good_title) in poller._seen_titles
