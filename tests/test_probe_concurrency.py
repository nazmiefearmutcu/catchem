"""Pin v45-critical-2: probe_feed_async must serialise with background tick.

The poller's manual probe used to take an asyncio lock nowhere — every
mutation of ``self._seen`` (an ``OrderedDict``), ``self.total_ingested``
(int), and ``self.last_error`` (str) was unguarded. A concurrent
background ``_run_one_tick`` running ``_poll_once`` mutated the SAME
fields. The race surfaces as one of:

  (a) silently lost dedup entries when both tasks hit ``_seen.add``
      concurrently — the LRU eviction can pop a key the other task
      just inserted because OrderedDict's ``__setitem__`` is not
      thread-safe across cooperative yields;
  (b) ``total_ingested`` increments lost because both tasks did
      ``self.total_ingested += n`` against the same observed value;
  (c) ``last_error`` ending up pointing at the wrong task's failure.

The fix wraps the entire ``probe_feed_async`` body in ``async with
self._lock:`` so it runs serially with any in-flight ``_run_one_tick``.
This test reproduces concurrent calls and asserts none of the race
symptoms occur.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from catchem import news_poller as news_poller_module
from catchem.news_poller import (
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
)

# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubStorage:
    """Storage stub: get_record always None so probe + tick BOTH see "new"."""

    def get_record(self, _cap_id: str) -> None:
        return None


class _StubSupervisor:
    """Supervisor stub: capture every process_capture call thread-safely.

    The probe and the background tick both call into ``process_capture``
    via ``asyncio.to_thread``. Without the fix these can interleave on
    the same item — we record them so the test can verify dedup didn't
    drop anything and no duplicate was emitted.
    """

    def __init__(self) -> None:
        self.storage = _StubStorage()
        self.processed: list[str] = []
        self._lock = asyncio.Lock()

    def process_capture(self, cap: Any) -> None:
        # Pure capture URL append — the stub runs on the worker thread
        # via ``asyncio.to_thread``. List append is GIL-atomic so we
        # don't need a lock here.
        self.processed.append(getattr(cap, "url", str(cap)))


class _StubSettings:
    class paths:
        catchem_output_dir = Path("/tmp")


def _make_poller(feeds: list[FeedSpec]) -> NewsPoller:
    sup = _StubSupervisor()
    poller = NewsPoller(
        supervisor=sup,  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=feeds,
    )
    # _ingest_one writes a jsonl archive copy; redirect to a no-op so
    # the stub stays pure.
    poller._ingest_one = lambda item: sup.process_capture(item)  # type: ignore[assignment]
    return poller


def _item(url: str) -> ParsedItem:
    return ParsedItem(
        title=f"Title for {url}",
        text=f"Body text for {url} — some plausible content.",
        url=url,
        domain="example.com",
        published_ts=datetime.now(UTC),
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_serialises_with_background_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ``probe_feed_async`` + ``_run_one_tick`` must not race.

    We fire two coroutines at once that BOTH ingest the same items.
    Under the fix each one runs its body under ``self._lock`` so they
    serialise; the combined ``total_ingested`` is exactly the number of
    distinct items (one task ingests them, the other dedups against
    ``self._seen``).
    """
    spec = FeedSpec("alpha", "https://alpha.example.com/rss", "alpha.example.com")
    poller = _make_poller([spec])

    items = tuple(_item(f"https://alpha.example.com/article-{i}") for i in range(8))

    async def _stub_fetch(
        _client: httpx.AsyncClient | None, _spec: FeedSpec
    ) -> FeedFetchResult:
        # Yield once so the event loop can interleave the two coroutines
        # at the precise instant the body would otherwise mutate _seen.
        await asyncio.sleep(0)
        return FeedFetchResult(
            spec=_spec,
            items=items,
            status_code=200,
            elapsed_ms=4.0,
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(news_poller_module, "fetch_feed_result", _stub_fetch)

    # Use a real httpx client so the poller's branch that exists with a
    # shared client is the one taken — closer to the production path.
    async with httpx.AsyncClient() as client:
        poller._client = client

        # Two probes back-to-back targeting the same feed; if the lock
        # works they run sequentially, second one sees everything in
        # ``_seen`` already and ingests nothing.
        results = await asyncio.gather(
            poller.probe_feed_async(spec.url),
            poller.probe_feed_async(spec.url),
        )

    # Both probes return a snapshot; no exception, no missing key.
    assert all(isinstance(r, dict) for r in results), results

    # Total ingested must equal the distinct items (8). Without the lock
    # we'd see <8 (lost-update) or >8 (double-count), depending on the
    # interleave.
    assert poller.total_ingested == len(items), (
        f"total_ingested={poller.total_ingested} but expected {len(items)} — "
        "indicates a race on the increment."
    )

    sup: _StubSupervisor = poller._sup  # type: ignore[assignment]
    # process_capture must be called exactly once per item.
    assert len(sup.processed) == len(items), (
        f"processed {len(sup.processed)} times but expected {len(items)} "
        f"(would indicate _seen dedup raced and emitted duplicates)."
    )
    assert set(sup.processed) == {i.url for i in items}, sup.processed


@pytest.mark.asyncio
async def test_probe_concurrent_with_run_one_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``probe_feed_async`` and ``_run_one_tick`` aimed at overlapping items.

    Background tick polls all configured feeds; probe targets one of
    them. Both ingest the same article set. With the lock both finish
    cleanly and the union of unique URLs equals the expected count;
    without the lock ``_seen`` corruption can lose entries or duplicate
    them.
    """
    spec_a = FeedSpec("alpha", "https://alpha.example.com/rss", "alpha.example.com")
    spec_b = FeedSpec("beta", "https://beta.example.com/rss", "beta.example.com")
    poller = _make_poller([spec_a, spec_b])

    items_a = tuple(_item(f"https://alpha.example.com/article-{i}") for i in range(4))
    items_b = tuple(_item(f"https://beta.example.com/article-{i}") for i in range(4))

    async def _stub_fetch(
        _client: httpx.AsyncClient | None, sp: FeedSpec
    ) -> FeedFetchResult:
        # Allow other tasks to run, forcing the race window open.
        await asyncio.sleep(0)
        items = items_a if sp.url == spec_a.url else items_b
        return FeedFetchResult(
            spec=sp,
            items=items,
            status_code=200,
            elapsed_ms=4.0,
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(news_poller_module, "fetch_feed_result", _stub_fetch)

    async with httpx.AsyncClient() as client:
        poller._client = client

        # _run_one_tick polls every feed, probe targets one specific URL.
        bg, probe = await asyncio.gather(
            poller._run_one_tick(client),
            poller.probe_feed_async(spec_a.url),
        )

    # bg is an int (count ingested), probe is a dict (health snapshot).
    assert isinstance(bg, int)
    assert isinstance(probe, dict)

    sup: _StubSupervisor = poller._sup  # type: ignore[assignment]
    unique_urls = set(sup.processed)
    expected = {i.url for i in (*items_a, *items_b)}
    assert unique_urls == expected, (
        f"missing urls: {expected - unique_urls}, extra: {unique_urls - expected}"
    )
    # No duplicates from the shared _seen.
    assert len(sup.processed) == len(unique_urls), (
        f"duplicate ingest detected: {sorted(sup.processed)}"
    )
