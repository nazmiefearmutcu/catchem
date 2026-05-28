"""Stable JSON contract + runtime semantics for /api/stats.

Pins:
  * the response envelope (schema_version + db + reviewers + ...),
  * that uptime is positive after sidecar boot,
  * that the request counter increments under load,
  * that the DB counts are zero on a fresh DB,
  * that deepseek_usd_spent surfaces the registry's cumulative spend.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import _REQUEST_COUNTS, _STATS_CACHE, create_app
from catchem.settings import load_settings, reload_settings


REQUIRED_TOP_KEYS = {
    "schema_version",
    "generated_at",
    "uptime_seconds",
    "request_counts",
    "total_requests",
    "db",
    "reviewers",
    "process",
    "version",
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    reload_settings()
    # Reset middleware counter + cache so each test sees a clean baseline.
    _REQUEST_COUNTS.clear()
    _STATS_CACHE["payload"] = None
    _STATS_CACHE["expires_at"] = 0.0
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_api_stats_returns_valid_schema(client: TestClient) -> None:
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    missing = REQUIRED_TOP_KEYS - set(body.keys())
    assert not missing, f"/api/stats missing top-level keys: {missing}"
    assert body["schema_version"] == 1
    assert isinstance(body["request_counts"], dict)
    assert isinstance(body["db"], dict)
    assert {"records", "reviews", "dlq"} <= set(body["db"].keys())
    assert isinstance(body["reviewers"], dict)
    assert {"deepseek_usd_spent", "stub_active"} <= set(body["reviewers"].keys())
    assert body["reviewers"]["stub_active"] is True
    # Process telemetry block — pin the shape so the OpsPage's
    # ``RuntimeStatsResponse.process`` type can never silently drift from
    # the wire. The numeric floors are loose because RSS varies wildly
    # between host machines / CI runners, but the contract is rigid.
    assert isinstance(body["process"], dict)
    assert {"rss_mb", "vms_mb", "cpu_percent", "num_threads", "psutil_available"} <= set(
        body["process"].keys()
    )
    assert isinstance(body["process"]["rss_mb"], (int, float))
    assert body["process"]["rss_mb"] >= 0.0
    assert isinstance(body["process"]["num_threads"], int)
    assert body["process"]["num_threads"] >= 0
    assert isinstance(body["process"]["psutil_available"], bool)


def test_api_stats_uptime_is_positive(client: TestClient) -> None:
    # A tiny sleep so the supervisor's start-time is provably in the past
    # under any wall-clock resolution.
    time.sleep(0.05)
    r = client.get("/api/stats")
    body = r.json()
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] > 0.0


def test_api_stats_request_counter_increments(client: TestClient) -> None:
    # Bust the TTL cache so each /api/stats hit re-reads the live counter.
    _STATS_CACHE["payload"] = None
    _STATS_CACHE["expires_at"] = 0.0

    # Warm up — known endpoint that the counter must observe.
    client.get("/healthz")
    client.get("/healthz")
    _STATS_CACHE["payload"] = None
    _STATS_CACHE["expires_at"] = 0.0
    before = client.get("/api/stats").json()
    healthz_before = int(before["request_counts"].get("/healthz", 0))
    assert healthz_before >= 2

    # Fire three more requests against the same path.
    for _ in range(3):
        client.get("/healthz")

    _STATS_CACHE["payload"] = None
    _STATS_CACHE["expires_at"] = 0.0
    after = client.get("/api/stats").json()
    healthz_after = int(after["request_counts"].get("/healthz", 0))
    assert healthz_after >= healthz_before + 3
    assert after["total_requests"] >= before["total_requests"] + 3


def test_api_stats_db_counts_zero_for_empty_db(client: TestClient) -> None:
    r = client.get("/api/stats")
    body = r.json()
    assert body["db"]["records"] == 0
    assert body["db"]["reviews"] == 0
    assert body["db"]["dlq"] == 0


def test_api_stats_deepseek_spent_reflects_registry(client: TestClient) -> None:
    # With no reviewer activity the cumulative spend is 0.0 regardless of
    # whether DeepSeek is even keyed. This pins the field's safe-default
    # behavior — a missing reviewer must NOT crash the endpoint.
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    spent = body["reviewers"]["deepseek_usd_spent"]
    assert isinstance(spent, (int, float))
    assert spent == 0.0

    # Now poke the registry's internal cumulative-spend cache and verify
    # the endpoint reflects the new value on the next cache miss.
    from catchem.api import _get_supervisor
    sup = _get_supervisor()
    registry = sup.reviewers
    # Hydrate the cache (may read 0.0 from an empty reviews table).
    registry.budget_state()
    registry._cached_spent_usd = 1.23  # noqa: SLF001 — test seam
    # Bust the /api/stats response cache so the new value is observed.
    _STATS_CACHE["payload"] = None
    _STATS_CACHE["expires_at"] = 0.0
    r2 = client.get("/api/stats")
    assert r2.json()["reviewers"]["deepseek_usd_spent"] == pytest.approx(1.23)
