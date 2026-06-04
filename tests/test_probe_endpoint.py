"""Contract tests for the manual per-feed probe endpoint.

Covers both layers:

  * ``NewsPoller.probe_feed_async`` fetches one configured URL, bypasses
    any active circuit-breaker cooldown, and updates feed_health.
  * ``POST /api/news/sources/probe`` returns 200 on a configured URL,
    400 when the URL is missing, 404 when it's not configured, and 503
    when no poller is wired into the app.

The probe pipeline reuses ``fetch_feed_result`` which is exercised with
a monkeypatched async stub so the test runs offline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem import news_poller as news_poller_module
from catchem.api import create_app
from catchem.news_poller import (
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
)
from catchem.rate_limit import DB_IMPORT_BUCKET, reset_all_buckets
from catchem.settings import load_settings, reload_settings

# ── Helpers ──────────────────────────────────────────────────────────────────


class _StubStorage:
    """Storage stub: get_record always None so dedup never short-circuits."""

    def get_record(self, _cap_id: str) -> None:
        return None


class _StubSupervisor:
    """Supervisor stub: process_capture is a no-op for probe tests."""

    def __init__(self) -> None:
        self.storage = _StubStorage()

    def process_capture(self, _cap: object) -> None:
        return None


class _StubSettings:
    class paths:
        catchem_output_dir = Path("/tmp")


def _make_poller(feeds: list[FeedSpec]) -> NewsPoller:
    return NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=feeds,
    )


def _ok_result(spec: FeedSpec, items: tuple[ParsedItem, ...] = ()) -> FeedFetchResult:
    return FeedFetchResult(
        spec=spec,
        items=items,
        status_code=200,
        elapsed_ms=11.0,
        fetched_at=datetime.now(UTC),
    )


def _err_result(spec: FeedSpec, status: int = 500) -> FeedFetchResult:
    return FeedFetchResult(
        spec=spec,
        status_code=status,
        error=f"http_{status}",
        elapsed_ms=4.0,
        fetched_at=datetime.now(UTC),
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_rate_buckets() -> None:
    """Clear in-memory rate-limit state between tests so the probe bucket
    starts at full capacity each run."""
    reset_all_buckets()
    yield
    reset_all_buckets()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


# ── Endpoint contract ───────────────────────────────────────────────────────


def test_probe_endpoint_missing_url_returns_400(client: TestClient) -> None:
    r = client.post("/api/news/sources/probe", json={})
    assert r.status_code == 400, r.text
    assert "url required" in r.text


def test_probe_endpoint_returns_503_when_no_poller(client: TestClient) -> None:
    """No poller configured at all → 503 distinguishes infra-down from 404."""
    r = client.post(
        "/api/news/sources/probe",
        json={"url": "https://example.com/rss"},
    )
    assert r.status_code == 503, r.text
    assert "news_poller_disabled" in r.text


def test_probe_endpoint_unknown_url_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a poller whose feeds don't include the requested URL."""
    from catchem import api as api_module

    spec = FeedSpec("known", "https://known.example.com/rss", "known.example.com")
    poller = _make_poller([spec])
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    r = client.post(
        "/api/news/sources/probe",
        json={"url": "https://unknown.example.com/rss"},
    )
    assert r.status_code == 404, r.text
    assert "feed not configured" in r.text


def test_probe_endpoint_happy_path_returns_updated_feed_health(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catchem import api as api_module

    spec = FeedSpec("ok", "https://ok.example.com/rss", "ok.example.com")
    poller = _make_poller([spec])

    async def _stub_fetch(_client, fed_spec):
        return _ok_result(fed_spec)

    monkeypatch.setattr(news_poller_module, "fetch_feed_result", _stub_fetch)
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    r = client.post("/api/news/sources/probe", json={"url": spec.url})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["url"] == spec.url
    result = body["result"]
    assert isinstance(result, dict)
    assert result["name"] == spec.name
    assert result["ok"] is True
    assert int(result["total_fetches"]) == 1


# ── Cooldown bypass ─────────────────────────────────────────────────────────


def test_probe_bypasses_cooldown_and_can_close_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feed in the middle of its cooldown window must still be probed
    when the manual button fires; a successful probe must clear the
    cooldown so the next regular tick re-considers the feed."""
    spec = FeedSpec("cold", "https://cold.example.com/rss", "cold.example.com")
    poller = _make_poller([spec])

    # Seed feed_health to look like the circuit-breaker just tripped.
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    poller.feed_health[spec.name] = {
        "name": spec.name,
        "url": spec.url,
        "fallback_domain": spec.fallback_domain,
        "ok": False,
        "backed_off": True,
        "status_code": 500,
        "error": "http_500",
        "item_count": 0,
        "items_total": 0,
        "last_fetch_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": None,
        "total_fetches": 5,
        "total_errors": 5,
        "consecutive_errors": 5,
        "cooldown_until": future,
        "last_success_at": None,
        "last_failure_at": datetime.now(UTC).isoformat(),
    }

    # Stub fetch returns a clean 200 — should reset consecutive_errors.
    async def _stub_fetch(_client, fed_spec):
        return _ok_result(fed_spec)

    monkeypatch.setattr(news_poller_module, "fetch_feed_result", _stub_fetch)

    updated = asyncio.run(poller.probe_feed_async(spec.url))

    assert updated["ok"] is True
    assert updated["cooldown_until"] is None
    assert updated["consecutive_errors"] == 0
    assert updated["backed_off"] is False
    # total_fetches must have ticked from 5 → 6 (probe counts as a real fetch).
    assert int(updated["total_fetches"]) == 6


def test_probe_failure_climbs_backoff_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed probe must still get folded into feed_health (total_errors
    increments, cooldown reschedules) instead of being a no-op."""
    spec = FeedSpec("flaky", "https://flaky.example.com/rss", "flaky.example.com")
    poller = _make_poller([spec])
    # Pre-seed at threshold so the next failure schedules a fresh cooldown.
    poller.feed_health[spec.name] = {
        "name": spec.name,
        "url": spec.url,
        "fallback_domain": spec.fallback_domain,
        "ok": False,
        "backed_off": False,
        "status_code": 500,
        "error": "http_500",
        "item_count": 0,
        "items_total": 0,
        "last_fetch_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": None,
        "total_fetches": 4,
        "total_errors": 4,
        "consecutive_errors": 4,
        "cooldown_until": None,
        "last_success_at": None,
        "last_failure_at": datetime.now(UTC).isoformat(),
    }

    async def _stub_fetch(_client, fed_spec):
        return _err_result(fed_spec, status=502)

    monkeypatch.setattr(news_poller_module, "fetch_feed_result", _stub_fetch)

    updated = asyncio.run(poller.probe_feed_async(spec.url))

    assert updated["ok"] is False
    assert updated["consecutive_errors"] == 5
    # Crossing the threshold schedules a non-null cooldown ISO timestamp.
    assert updated["cooldown_until"] is not None
    assert int(updated["total_errors"]) == 5


def test_probe_endpoint_rate_limit_blocks_after_burst(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe bucket caps a runaway loop — once exhausted, further
    POSTs return 429 with a Retry-After header."""
    from catchem import api as api_module

    spec = FeedSpec("rate", "https://rate.example.com/rss", "rate.example.com")
    poller = _make_poller([spec])

    async def _stub_fetch(_client, fed_spec):
        return _ok_result(fed_spec)

    monkeypatch.setattr(news_poller_module, "fetch_feed_result", _stub_fetch)
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    # DB_IMPORT_BUCKET capacity is 6 at cost=1 — burn through it.
    last_status: int | None = None
    for _ in range(DB_IMPORT_BUCKET.capacity + 1):
        r = client.post("/api/news/sources/probe", json={"url": spec.url})
        last_status = r.status_code
    assert last_status == 429
