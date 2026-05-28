"""Circuit-breaker tests for the news poller.

Pin the ladder, the threshold, the cooldown bookkeeping, and the
end-to-end behaviour where consecutive failures cause cooldowns and a
single success closes the breaker. We never make real network calls
here — every test drives `_record_feed_result` or stubs
`fetch_feed_result` so the asyncio loop / HTTP client are not exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catchem.news_poller import (
    BACKOFF_LADDER_SECONDS,
    CIRCUIT_BREAKER_THRESHOLD,
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    _compute_cooldown_until,
)


# ── ladder + threshold contract ──────────────────────────────────────────────


def test_backoff_ladder_is_monotonically_increasing() -> None:
    """If the ladder isn't monotone an outage at step N gets retried sooner
    than at step N-1, which defeats the breaker."""
    assert list(BACKOFF_LADDER_SECONDS) == sorted(BACKOFF_LADDER_SECONDS)
    assert BACKOFF_LADDER_SECONDS[0] == 60      # 1m
    assert BACKOFF_LADDER_SECONDS[-1] == 3600   # 60m (cap)


def test_circuit_breaker_threshold_is_five() -> None:
    """Documented behaviour: 5 consecutive failures opens the breaker."""
    assert CIRCUIT_BREAKER_THRESHOLD == 5


# ── _compute_cooldown_until pure-function checks ─────────────────────────────


def test_compute_cooldown_returns_none_below_threshold() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    for n in range(0, CIRCUIT_BREAKER_THRESHOLD):
        assert _compute_cooldown_until(n, now) is None, n


def test_compute_cooldown_at_threshold_uses_first_rung() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    got = _compute_cooldown_until(CIRCUIT_BREAKER_THRESHOLD, now)
    assert got is not None
    assert got == now + timedelta(seconds=BACKOFF_LADDER_SECONDS[0])


def test_compute_cooldown_climbs_each_step() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    # 6 errors → ladder index 1 (300s), 9 errors → ladder index 4 (3600s).
    six = _compute_cooldown_until(6, now)
    nine = _compute_cooldown_until(9, now)
    assert six == now + timedelta(seconds=BACKOFF_LADDER_SECONDS[1])
    assert nine == now + timedelta(seconds=BACKOFF_LADDER_SECONDS[4])


def test_compute_cooldown_caps_at_last_ladder_step() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    # 10+ errors stays pinned at the top of the ladder, not "x * 3600".
    for n in (10, 25, 999):
        got = _compute_cooldown_until(n, now)
        assert got == now + timedelta(seconds=BACKOFF_LADDER_SECONDS[-1]), n


# ── shared poller stub ───────────────────────────────────────────────────────


def _make_poller(feeds: list[FeedSpec]) -> NewsPoller:
    class _StubSupervisor:
        pass

    class _StubSettings:
        class paths:
            catchem_output_dir = Path("/tmp")

    return NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=feeds,
    )


# ── _record_feed_result bookkeeping ──────────────────────────────────────────


def test_four_consecutive_failures_do_not_set_cooldown() -> None:
    spec = FeedSpec("flaky", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    for _ in range(4):
        poller._record_feed_result(FeedFetchResult(spec=spec, status_code=503, error="http_503"))
    snap = poller.feed_health_snapshot()[0]
    assert snap["consecutive_errors"] == 4
    assert snap["cooldown_until"] is None
    assert snap["backed_off"] is False


def test_five_consecutive_failures_opens_circuit_at_first_rung() -> None:
    spec = FeedSpec("broken", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    for _ in range(5):
        poller._record_feed_result(FeedFetchResult(
            spec=spec, status_code=503, error="http_503", fetched_at=fixed_now,
        ))
    snap = poller.feed_health_snapshot()[0]
    assert snap["consecutive_errors"] == 5
    cooldown = datetime.fromisoformat(str(snap["cooldown_until"]))
    expected = fixed_now + timedelta(seconds=BACKOFF_LADDER_SECONDS[0])
    assert cooldown == expected


def test_six_failures_uses_second_ladder_step() -> None:
    spec = FeedSpec("broken", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    for _ in range(6):
        poller._record_feed_result(FeedFetchResult(
            spec=spec, status_code=503, error="http_503", fetched_at=fixed_now,
        ))
    cooldown = datetime.fromisoformat(str(poller.feed_health_snapshot()[0]["cooldown_until"]))
    assert cooldown == fixed_now + timedelta(seconds=BACKOFF_LADDER_SECONDS[1])


def test_nine_failures_uses_top_ladder_step() -> None:
    spec = FeedSpec("broken", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    for _ in range(9):
        poller._record_feed_result(FeedFetchResult(
            spec=spec, status_code=503, error="http_503", fetched_at=fixed_now,
        ))
    cooldown = datetime.fromisoformat(str(poller.feed_health_snapshot()[0]["cooldown_until"]))
    assert cooldown == fixed_now + timedelta(seconds=BACKOFF_LADDER_SECONDS[4])


def test_more_than_nine_failures_stays_at_top_ladder_step() -> None:
    spec = FeedSpec("broken", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    for _ in range(15):
        poller._record_feed_result(FeedFetchResult(
            spec=spec, status_code=503, error="http_503", fetched_at=fixed_now,
        ))
    cooldown = datetime.fromisoformat(str(poller.feed_health_snapshot()[0]["cooldown_until"]))
    # Capped at 3600s — does NOT keep climbing past the ladder length.
    assert cooldown == fixed_now + timedelta(seconds=BACKOFF_LADDER_SECONDS[-1])


def test_success_resets_consecutive_errors_and_clears_cooldown() -> None:
    spec = FeedSpec("recovers", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    for _ in range(7):
        poller._record_feed_result(FeedFetchResult(
            spec=spec, status_code=502, error="http_502", fetched_at=fixed_now,
        ))
    pre = poller.feed_health_snapshot()[0]
    assert pre["consecutive_errors"] == 7
    assert pre["cooldown_until"] is not None
    # Feed comes back.
    poller._record_feed_result(FeedFetchResult(
        spec=spec, status_code=200, elapsed_ms=10.0, fetched_at=fixed_now + timedelta(minutes=10),
    ))
    post = poller.feed_health_snapshot()[0]
    assert post["consecutive_errors"] == 0
    assert post["cooldown_until"] is None
    assert post["ok"] is True
    assert post["backed_off"] is False


# ── _poll_once cooldown gate (no network, fully mocked) ──────────────────────


@pytest.mark.asyncio
async def test_poll_once_skips_feeds_currently_in_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """During the cooldown window the poller must NOT issue a fetch.

    We monkeypatch `fetch_feed_result` in the module so we can assert it
    is never called for a backed-off feed, and `Supervisor.process_capture`
    is never reached either.
    """
    from catchem import news_poller as np_module

    spec = FeedSpec("dead", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    future = datetime.now(UTC) + timedelta(seconds=600)
    # Seed health as if the breaker is already open.
    poller.feed_health[spec.name] = {
        "name": spec.name,
        "url": spec.url,
        "fallback_domain": spec.fallback_domain,
        "ok": False,
        "backed_off": True,
        "status_code": 503,
        "error": "http_503",
        "item_count": 0,
        "items_total": 0,
        "last_fetch_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": None,
        "total_fetches": 7,
        "total_errors": 7,
        "consecutive_errors": 7,
        "cooldown_until": future.isoformat(),
        "last_success_at": None,
        "last_failure_at": datetime.now(UTC).isoformat(),
    }

    fetch_calls: list[str] = []

    async def _spy_fetch(_client, s: FeedSpec) -> FeedFetchResult:
        fetch_calls.append(s.name)
        return FeedFetchResult(spec=s, status_code=200, elapsed_ms=1.0)

    monkeypatch.setattr(np_module, "fetch_feed_result", _spy_fetch)

    class _DummyClient:
        pass

    ingested = await poller._poll_once(_DummyClient())  # type: ignore[arg-type]
    assert ingested == 0
    assert fetch_calls == []
    snap = poller.feed_health_snapshot()[0]
    # Skipped tick must preserve consecutive_errors + cooldown_until so the
    # next tick still respects the breaker.
    assert snap["consecutive_errors"] == 7
    assert snap["cooldown_until"] == future.isoformat()
    assert snap["backed_off"] is True
    # And the polls/errors counters MUST NOT have moved.
    assert snap["total_fetches"] == 7
    assert snap["total_errors"] == 7


@pytest.mark.asyncio
async def test_poll_once_probes_again_after_cooldown_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the cooldown timestamp is in the past the next tick MUST fetch."""
    from catchem import news_poller as np_module

    spec = FeedSpec("revival", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    past = datetime.now(UTC) - timedelta(seconds=5)
    poller.feed_health[spec.name] = {
        "name": spec.name,
        "url": spec.url,
        "fallback_domain": spec.fallback_domain,
        "ok": False,
        "backed_off": True,
        "status_code": 503,
        "error": "http_503",
        "item_count": 0,
        "items_total": 0,
        "last_fetch_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": None,
        "total_fetches": 5,
        "total_errors": 5,
        "consecutive_errors": 5,
        "cooldown_until": past.isoformat(),
        "last_success_at": None,
        "last_failure_at": datetime.now(UTC).isoformat(),
    }

    fetch_calls: list[str] = []

    async def _spy_fetch(_client, s: FeedSpec) -> FeedFetchResult:
        fetch_calls.append(s.name)
        # Simulate the feed coming back online.
        return FeedFetchResult(spec=s, status_code=200, elapsed_ms=5.0)

    monkeypatch.setattr(np_module, "fetch_feed_result", _spy_fetch)

    class _DummyClient:
        pass

    await poller._poll_once(_DummyClient())  # type: ignore[arg-type]
    assert fetch_calls == ["revival"]
    snap = poller.feed_health_snapshot()[0]
    # Successful probe must close the breaker AND increment polls counter.
    assert snap["consecutive_errors"] == 0
    assert snap["cooldown_until"] is None
    assert snap["backed_off"] is False
    assert snap["total_fetches"] == 6


def test_skipped_result_preserves_existing_health_snapshot() -> None:
    """_record_feed_result with skipped=True must not destroy prior state."""
    spec = FeedSpec("paused", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    # Drive 5 failures so the breaker opens.
    for _ in range(5):
        poller._record_feed_result(FeedFetchResult(
            spec=spec, status_code=503, error="http_503", fetched_at=fixed_now,
        ))
    pre = dict(poller.feed_health_snapshot()[0])
    assert pre["consecutive_errors"] == 5
    # Now feed in a skipped-result and check state is preserved.
    poller._record_feed_result(FeedFetchResult(
        spec=spec, skipped=True, fetched_at=fixed_now + timedelta(seconds=10),
    ))
    post = poller.feed_health_snapshot()[0]
    assert post["consecutive_errors"] == 5
    assert post["total_fetches"] == 5  # NOT incremented
    assert post["total_errors"] == 5   # NOT incremented
    assert post["backed_off"] is True
    assert post["cooldown_until"] == pre["cooldown_until"]
