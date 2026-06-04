"""Adaptive per-source polling for the news poller.

The poller fetches every configured feed each cycle. Some feeds (GDELT,
Google News, squawk-style) yield fresh items almost every cycle; others
(think-tanks, podcasts, quiet regulators) return HTTP 200 but ZERO new
items for hours. Polling the dry ones every 10s wastes bandwidth and drags
the median publisher-lag window. This module adds an adaptive cadence:
persistently-empty feeds back off to a longer cycle multiplier while
high-yield feeds keep fetching every cycle.

This is SEPARATE from the error circuit breaker (test_circuit_breaker.py),
which reacts to *failures* (5xx / timeouts), not *emptiness*.

These tests pin:
  * `_adaptive_cadence` — the pure ladder mapping consecutive-empty → multiplier.
  * the `_poll_once` gate — an empty feed is skipped on non-due cycles and
    re-fetched once due; a yielding feed stays every-cycle.
  * the disabled setting — poll every feed every cycle (today's behavior).
  * the per-feed telemetry (consecutive_empty / adaptive_cadence / total_new_items).
  * errors do NOT advance the emptiness ladder (breaker's domain).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import catchem.news_poller as np
from catchem.news_poller import (
    ADAPTIVE_CADENCE_MAX,
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
    _adaptive_cadence,
)

# ──────────────────────────────────────────────────────────────────────────────
# _adaptive_cadence ladder (pure function)
# ──────────────────────────────────────────────────────────────────────────────


def test_adaptive_cadence_every_cycle_for_low_empty_counts() -> None:
    # 0-2 consecutive empties → still poll every cycle (multiplier 1).
    assert _adaptive_cadence(0) == 1
    assert _adaptive_cadence(1) == 1
    assert _adaptive_cadence(2) == 1


def test_adaptive_cadence_third_rung_at_three_to_five() -> None:
    # 3-5 empties → every 3rd cycle.
    assert _adaptive_cadence(3) == 3
    assert _adaptive_cadence(4) == 3
    assert _adaptive_cadence(5) == 3


def test_adaptive_cadence_sixth_rung_at_six_to_ten() -> None:
    # 6-10 empties → every 6th cycle.
    assert _adaptive_cadence(6) == 6
    assert _adaptive_cadence(9) == 6
    assert _adaptive_cadence(10) == 6


def test_adaptive_cadence_caps_above_ten() -> None:
    # >10 empties → every 12th cycle (the cap), never higher.
    assert _adaptive_cadence(11) == ADAPTIVE_CADENCE_MAX == 12
    assert _adaptive_cadence(50) == 12
    assert _adaptive_cadence(9999) == 12


def test_adaptive_cadence_is_monotonic_non_decreasing() -> None:
    # The longer a feed stays empty, the rarer (never sooner) we poll it.
    seq = [_adaptive_cadence(n) for n in range(0, 30)]
    assert seq == sorted(seq)
    assert seq[0] == 1 and seq[-1] == 12


def test_adaptive_cadence_clamps_negative_to_every_cycle() -> None:
    # Defensive: a negative count is treated as "fresh" (multiplier 1).
    assert _adaptive_cadence(-1) == 1
    assert _adaptive_cadence(-100) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Stubs + helpers (mirror test_news_poller_dedup.py)
# ──────────────────────────────────────────────────────────────────────────────


class _StubStorage:
    """No record ever pre-exists → an item with a fresh URL is always 'new'."""

    def get_record(self, _cap_id: str) -> None:
        return None


class _StubSupervisor:
    def __init__(self) -> None:
        self.storage = _StubStorage()
        self.processed: list[object] = []

    def process_capture(self, cap: object) -> None:
        self.processed.append(cap)


def _make_settings(tmp_path: Path, *, adaptive: bool):
    class _Paths:
        catchem_output_dir = tmp_path

    class _News:
        # Title dedup OFF so emptiness is the only thing under test here.
        dedup_title_window_seconds = 0.0
        adaptive_polling_enabled = adaptive

    class _Settings:
        paths = _Paths()
        news = _News()

    return _Settings()


def _item(url: str, *, when: datetime, title: str | None = None) -> ParsedItem:
    return ParsedItem(
        title=title or f"Story at {url}",
        text=f"body for {url}",
        url=url,
        domain="example.com",
        published_ts=when,
    )


def _make_poller(tmp_path: Path, feeds: list[FeedSpec], *, adaptive: bool) -> NewsPoller:
    return NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_make_settings(tmp_path, adaptive=adaptive),  # type: ignore[arg-type]
        feeds=feeds,
    )


def _install_fetch(monkeypatch, plan: dict[str, list[FeedFetchResult]], calls: list[str]) -> None:
    """Patch `fetch_feed_result` to pop a queued result per feed per call.

    `plan[name]` is a per-cycle queue of results for that feed. Each fetch
    records the feed name in `calls` (so we can assert which feeds were
    actually polled) and returns the next queued result. A feed that the
    adaptive gate skips never appears in `calls`.
    """

    async def _fake_fetch(_client, spec: FeedSpec) -> FeedFetchResult:
        calls.append(spec.name)
        queue = plan[spec.name]
        return queue.pop(0)

    monkeypatch.setattr(np, "fetch_feed_result", _fake_fetch)


def _ok(spec: FeedSpec, items: tuple[ParsedItem, ...]) -> FeedFetchResult:
    return FeedFetchResult(spec=spec, items=items, status_code=200)


def _err(spec: FeedSpec) -> FeedFetchResult:
    return FeedFetchResult(spec=spec, status_code=503, error="http_503")


# ──────────────────────────────────────────────────────────────────────────────
# _poll_once adaptive gate behavior
# ──────────────────────────────────────────────────────────────────────────────


def test_empty_feed_backs_off_and_is_skipped_then_refetched(monkeypatch, tmp_path) -> None:
    """A feed returning zero new items climbs to the 3rd-cycle rung, gets
    skipped on the in-between cycles, then is re-fetched once due."""
    spec = FeedSpec("quiet", "https://quiet.example/rss", "quiet.example")
    # Always returns OK-but-empty.
    plan = {"quiet": [_ok(spec, ()) for _ in range(20)]}
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    # Cycles 0,1,2 → all fetched (consecutive_empty 0→1→2 keeps cadence 1).
    for _ in range(3):
        asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert calls == ["quiet", "quiet", "quiet"]
    snap = poller.feed_health[spec.name]
    assert snap["consecutive_empty"] == 3
    assert snap["adaptive_cadence"] == 3  # now backed off to every-3rd

    # After cycle 2 (the 3rd fetch) cadence became 3 → next_due = 2 + 3 = 5.
    # Cycles 3 and 4 must be SKIPPED (3 < 5, 4 < 5); cycle 5 re-fetches.
    calls.clear()
    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]  # cycle 3
    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]  # cycle 4
    assert calls == [], "backed-off feed must be skipped on non-due cycles"
    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]  # cycle 5
    assert calls == ["quiet"], "feed must be re-fetched once it is due again"


def test_skipped_cycles_do_not_touch_health_counters(monkeypatch, tmp_path) -> None:
    """When the adaptive gate skips a feed it neither succeeds nor fails, so
    total_fetches / total_errors / consecutive_empty stay frozen (unlike the
    cooldown breaker, the not-due skip writes no synthetic result)."""
    spec = FeedSpec("quiet", "https://quiet.example/rss", "quiet.example")
    plan = {"quiet": [_ok(spec, ()) for _ in range(10)]}
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    for _ in range(3):  # cycles 0,1,2 → fetched, cadence climbs to 3
        asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    frozen = dict(poller.feed_health[spec.name])
    assert frozen["total_fetches"] == 3

    calls.clear()
    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]  # cycle 3 (skipped)
    assert calls == []
    after = poller.feed_health[spec.name]
    # Nothing moved — the skip is invisible to the per-feed counters.
    assert after["total_fetches"] == frozen["total_fetches"]
    assert after["total_errors"] == frozen["total_errors"]
    assert after["consecutive_empty"] == frozen["consecutive_empty"]


def test_yielding_feed_stays_every_cycle(monkeypatch, tmp_path) -> None:
    """A feed that yields >=1 new item each cycle never backs off."""
    spec = FeedSpec("busy", "https://busy.example/rss", "busy.example")
    # Each cycle returns a DIFFERENT fresh URL → always >=1 new item.
    plan = {
        "busy": [
            _ok(spec, (_item(f"https://busy.example/{i}", when=datetime.now(UTC)),))
            for i in range(8)
        ]
    }
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    for _ in range(8):
        asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert calls == ["busy"] * 8, "a yielding feed must be polled every cycle"
    snap = poller.feed_health[spec.name]
    assert snap["consecutive_empty"] == 0
    assert snap["adaptive_cadence"] == 1
    assert snap["total_new_items"] == 8


def test_yield_resets_consecutive_empty_back_to_every_cycle(monkeypatch, tmp_path) -> None:
    """A single yielding cycle snaps a backed-off feed straight back to every-cycle."""
    spec = FeedSpec("mostly-quiet", "https://mq.example/rss", "mq.example")
    plan = {
        "mostly-quiet": [
            _ok(spec, ()),  # cycle 0: empty (ce=1)
            _ok(spec, ()),  # cycle 1: empty (ce=2)
            _ok(spec, ()),  # cycle 2: empty (ce=3 → cadence 3, next_due=5)
            # cycles 3,4 skipped; cycle 5 yields
            _ok(spec, (_item("https://mq.example/new", when=datetime.now(UTC)),)),
            _ok(spec, ()),  # cycle 6 after reset: empty again (ce=1)
        ]
    }
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    for _ in range(6):  # cycles 0..5 (two of them skipped, so 4 real fetches)
        asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    snap = poller.feed_health[spec.name]
    assert snap["consecutive_empty"] == 0, "yield must reset the empty streak"
    assert snap["adaptive_cadence"] == 1, "and snap cadence back to every-cycle"
    assert snap["total_new_items"] == 1
    # cycle 6 now runs (we're due again because cadence reset to 1).
    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert poller.feed_health[spec.name]["consecutive_empty"] == 1


def test_errors_do_not_advance_emptiness_ladder(monkeypatch, tmp_path) -> None:
    """A failed fetch is the circuit breaker's job, NOT emptiness — it must
    leave consecutive_empty / adaptive_cadence untouched."""
    spec = FeedSpec("flaky", "https://flaky.example/rss", "flaky.example")
    plan = {"flaky": [_err(spec) for _ in range(4)]}
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    for _ in range(4):
        asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    snap = poller.feed_health[spec.name]
    # Errors accrue on the breaker axis, not the emptiness axis.
    assert snap["consecutive_errors"] == 4
    assert snap["consecutive_empty"] == 0
    assert snap["adaptive_cadence"] == 1
    # Every error cycle was still fetched (the breaker only opens at 5).
    assert calls == ["flaky"] * 4


# ──────────────────────────────────────────────────────────────────────────────
# Disabled setting = poll-all (today's behavior)
# ──────────────────────────────────────────────────────────────────────────────


def test_disabled_setting_polls_every_feed_every_cycle(monkeypatch, tmp_path) -> None:
    """With adaptive_polling_enabled=False an always-empty feed is STILL
    fetched every single cycle — exactly the pre-feature behavior."""
    spec = FeedSpec("quiet", "https://quiet.example/rss", "quiet.example")
    # 12 cycles → consecutive_empty climbs to 12 (>10), exercising the cap.
    plan = {"quiet": [_ok(spec, ()) for _ in range(12)]}
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=False)
    assert poller._adaptive_polling_enabled is False

    for _ in range(12):
        asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert calls == ["quiet"] * 12, "disabled → never skip, poll every cycle"
    # Telemetry is STILL maintained for the UI even when scheduling is off.
    snap = poller.feed_health[spec.name]
    assert snap["consecutive_empty"] == 12
    assert snap["adaptive_cadence"] == ADAPTIVE_CADENCE_MAX  # surfaced, just not enforced


def test_adaptive_enabled_by_default_via_getattr_fallback(tmp_path) -> None:
    """A settings stub lacking `news.adaptive_polling_enabled` must default to
    enabled (matches NewsConfig default) without crashing construction."""

    class _Paths:
        catchem_output_dir = tmp_path

    class _News:
        pass  # no adaptive_polling_enabled attribute

    class _Settings:
        paths = _Paths()
        news = _News()

    poller = NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_Settings(),  # type: ignore[arg-type]
        feeds=[],
    )
    assert poller._adaptive_polling_enabled is True


def test_settings_default_has_adaptive_polling_enabled() -> None:
    from catchem.settings import NewsConfig

    assert NewsConfig().adaptive_polling_enabled is True


# ──────────────────────────────────────────────────────────────────────────────
# Telemetry keys present alongside existing feed_health keys
# ──────────────────────────────────────────────────────────────────────────────


def test_telemetry_keys_present_and_existing_keys_kept(monkeypatch, tmp_path) -> None:
    spec = FeedSpec("feed", "https://feed.example/rss", "feed.example")
    plan = {"feed": [_ok(spec, (_item("https://feed.example/1", when=datetime.now(UTC)),))]}
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    snap = poller.feed_health[spec.name]
    # New adaptive telemetry keys.
    for key in ("consecutive_empty", "adaptive_cadence", "total_new_items"):
        assert key in snap, key
    # Existing circuit-breaker / health keys must still be present.
    for key in (
        "name", "url", "ok", "item_count", "items_total",
        "total_fetches", "total_errors", "consecutive_errors", "cooldown_until",
    ):
        assert key in snap, key
    assert snap["total_new_items"] == 1
    assert snap["consecutive_empty"] == 0
    assert snap["adaptive_cadence"] == 1


def test_total_new_items_accumulates_across_cycles(monkeypatch, tmp_path) -> None:
    spec = FeedSpec("feed", "https://feed.example/rss", "feed.example")
    # Cycle 0: two new items, cycle 1: one new item.
    now = datetime.now(UTC)
    plan = {
        "feed": [
            _ok(spec, (
                _item("https://feed.example/1", when=now),
                _item("https://feed.example/2", when=now),
            )),
            _ok(spec, (_item("https://feed.example/3", when=now),)),
        ]
    }
    calls: list[str] = []
    _install_fetch(monkeypatch, plan, calls)
    poller = _make_poller(tmp_path, [spec], adaptive=True)

    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert poller.feed_health[spec.name]["total_new_items"] == 2
    asyncio.run(poller._poll_once(client=None))  # type: ignore[arg-type]
    assert poller.feed_health[spec.name]["total_new_items"] == 3
