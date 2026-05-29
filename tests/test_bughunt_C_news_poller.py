"""Regression tests for the C-news-poller bug-hunt group.

Three confirmed bugs in ``src/catchem/news_poller.py``:

  1. ``poll_now()``'s startup-grace branch ran a tick WITHOUT acquiring
     ``self._lock``, racing the shared dedup OrderedDicts / counters that
     the docstring and ``probe_feed_async`` promise are lock-serialized.
  2. ``fetch_feed_result`` passed a bare ``timeout=12.0`` on the request,
     which httpx applies in preference to the pinned ``_HTTPX_TIMEOUT``
     (connect=3/read=5), letting a stalled feed outlast the poll interval.
  3. The Atom ``<link>`` extractor in ``parse_feed`` took the FIRST <link>
     child regardless of ``rel``, so entries whose first link is
     self/edit/enclosure emitted the wrong article URL (also poisoning the
     dedup key + capture_id built from that URL).

Each test FAILS against the pre-fix code and PASSES after.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from catchem.news_poller import (
    _HTTPX_TIMEOUT,
    FeedSpec,
    NewsPoller,
    fetch_feed_result,
    parse_feed,
)


# ── Finding 3: Atom <link rel='alternate'> selection ────────────────────────

def test_atom_link_prefers_alternate_over_first_child() -> None:
    """An entry whose FIRST <link> is rel='self' must still yield the
    rel='alternate' canonical article URL, not the self/edit link."""
    body = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>SEC charges firm with fraud</title>
        <link rel="self" href="https://api.example.com/v1/entries/123.json"/>
        <link rel="edit" href="https://api.example.com/v1/entries/123/edit"/>
        <link rel="alternate" href="https://www.example.com/news/sec-charges-firm"/>
        <summary>The SEC announced charges today against the firm.</summary>
        <updated>2026-05-29T12:00:00Z</updated>
      </entry>
    </feed>"""
    items = parse_feed(body, fallback_domain="example.com")
    assert len(items) == 1
    # Pre-fix: url would be the rel='self' API link.
    assert items[0].url == "https://www.example.com/news/sec-charges-firm"
    # Domain is derived from the (now-correct) alternate URL's host. The parser
    # normalizes a leading www. away, so assert the registrable domain.
    assert items[0].domain == "example.com"


def test_atom_link_falls_back_to_first_when_no_alternate() -> None:
    """If no link qualifies as alternate/rel-less, fall back to the first
    link (preserve prior behavior for that shape)."""
    body = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Only a self link</title>
        <link rel="self" href="https://api.example.com/v1/entries/9.json"/>
        <summary>Body text here.</summary>
        <updated>2026-05-29T12:00:00Z</updated>
      </entry>
    </feed>"""
    items = parse_feed(body, fallback_domain="example.com")
    assert len(items) == 1
    assert items[0].url == "https://api.example.com/v1/entries/9.json"


def test_atom_rel_less_link_is_treated_as_alternate() -> None:
    """A <link> with no rel attribute (Atom default = alternate) wins over a
    later/earlier rel='enclosure'."""
    body = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Story</title>
        <link rel="enclosure" href="https://cdn.example.com/audio.mp3"/>
        <link href="https://www.example.com/story"/>
        <summary>Body text.</summary>
        <updated>2026-05-29T12:00:00Z</updated>
      </entry>
    </feed>"""
    items = parse_feed(body, fallback_domain="example.com")
    assert len(items) == 1
    assert items[0].url == "https://www.example.com/story"


# ── Finding 2: request-level timeout must not override the pinned one ────────

def test_fetch_feed_result_uses_pinned_timeout_not_bare_12s() -> None:
    """fetch_feed_result must not inflate the request timeout to 12s.

    httpx applies a request-level ``timeout`` in preference to the client
    default; a bare ``12.0`` expands connect/read/write/pool all to 12s,
    longer than the 10s interval floor. The fix keeps read<=5s.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, content=b"<rss><channel></channel></rss>")

    transport = httpx.MockTransport(handler)
    spec = FeedSpec(name="t", url="https://feed.example.com/rss", fallback_domain="example.com")

    async def run() -> None:
        # Client carries the pinned timeout exactly as the poller wires it.
        async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, transport=transport) as client:
            await fetch_feed_result(client, spec)

    asyncio.run(run())

    eff = captured["timeout"]
    assert isinstance(eff, dict)
    # The bug would surface as 12.0 across the board.
    assert eff["read"] == pytest.approx(5.0)
    assert eff["connect"] == pytest.approx(3.0)
    assert eff["read"] < 10.0  # must finish under the interval floor


# ── Finding 1: poll_now() startup-grace branch must hold the lock ────────────

class _FakeStorage:
    def get_record(self, _cap_id: str) -> None:
        return None


class _FakeSupervisor:
    """Minimal Supervisor stand-in; the poll_now lock test never ingests."""

    def __init__(self) -> None:
        self.storage = _FakeStorage()


def test_poll_now_grace_path_serializes_on_lock(tmp_settings) -> None:
    """During startup grace (self._client is None) poll_now must acquire
    self._lock so two concurrent presses cannot run unlocked ticks that
    corrupt shared OrderedDict state.

    We assert the lock is held *inside* the tick by stubbing _run_one_tick
    to observe self._lock.locked(). Pre-fix the grace branch never touched
    the lock, so locked() would be False there.
    """
    poller = NewsPoller(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=tmp_settings,
        feeds=(),
        interval_seconds=10.0,
        startup_grace_seconds=3.0,
    )
    assert poller._client is None  # grace window: ephemeral-client branch

    observed_locked: list[bool] = []

    async def fake_tick(_client: httpx.AsyncClient) -> int:
        observed_locked.append(poller._lock.locked())
        return 0

    poller._run_one_tick = fake_tick  # type: ignore[method-assign]

    async def run() -> None:
        # Two concurrent grace-window presses; both must serialize on the lock.
        await asyncio.gather(poller.poll_now(), poller.poll_now())

    asyncio.run(run())

    assert observed_locked == [True, True], (
        "poll_now grace path ran a tick without holding self._lock"
    )


def test_poll_now_grace_path_runs_ticks_sequentially_not_concurrently(tmp_settings) -> None:
    """Stronger race proof: with the lock held, two grace-window presses run
    one-at-a-time. Without the lock, both ticks would overlap (max concurrency 2).
    """
    poller = NewsPoller(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=tmp_settings,
        feeds=(),
        startup_grace_seconds=3.0,
    )
    assert poller._client is None

    state = {"active": 0, "max_active": 0}

    async def fake_tick(_client: httpx.AsyncClient) -> int:
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.02)  # widen the window for overlap to show
        state["active"] -= 1
        return 0

    poller._run_one_tick = fake_tick  # type: ignore[method-assign]

    async def run() -> None:
        await asyncio.gather(poller.poll_now(), poller.poll_now())

    asyncio.run(run())

    assert state["max_active"] == 1, (
        f"grace-window ticks overlapped (max_active={state['max_active']}); "
        "lock not serializing"
    )
