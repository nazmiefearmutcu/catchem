"""Contract + behavior tests for the deep health probe at /api/health/deep.

The endpoint is the readiness signal an external supervisor (K8s, Tauri
boot-shim, watchdog cron) reads to decide whether the sidecar is *ready*
to serve traffic — distinct from the liveness check at /healthz, which
just confirms the process is alive.

Tests pin:
  * happy path returns 200 + ok:true + the full ``checks`` dict
  * the response envelope is stable (5 keys, schema_version=1)
  * 503 + ok:false when SQLite fails (monkeypatched storage)
  * 503 + ok:false when the schema is "outdated" (monkeypatched max_known)
  * the news-poller stale-detection threshold reads as 5x interval
  * /healthz remains the simple {"status":"ok"} contract
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem import api as api_mod
from catchem.api import create_app
from catchem.settings import load_settings, reload_settings

REQUIRED_TOP_KEYS = {"ok", "checks", "issues", "generated_at", "schema_version"}
EXPECTED_SUBSYSTEM_KEYS = {
    "uptime_ok",
    "sqlite_ok",
    "news_poller_ok",
    "schema_ok",
    "disk_ok",
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    # News poller off by default — happy path needs deterministic checks.
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_deep_health_happy_path_returns_200_ok_true(client: TestClient) -> None:
    """All subsystems pass on a fresh sidecar => 200 + ok:true + no issues."""
    r = client.get("/api/health/deep")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["issues"] == []
    assert body["schema_version"] == 1


def test_deep_health_envelope_is_stable(client: TestClient) -> None:
    """The response envelope must always carry the same top-level keys
    and the ``checks`` dict must include each of the 5 subsystem flags."""
    r = client.get("/api/health/deep")
    body = r.json()
    missing = REQUIRED_TOP_KEYS - set(body.keys())
    assert not missing, f"/api/health/deep missing top-level keys: {missing}"
    assert isinstance(body["checks"], dict)
    assert isinstance(body["issues"], list)
    # All 5 subsystem ok flags must be present (truth-y or falsy is fine,
    # but the *key* is a contract for downstream alerting).
    missing_checks = EXPECTED_SUBSYSTEM_KEYS - set(body["checks"].keys())
    assert not missing_checks, f"checks missing subsystem keys: {missing_checks}"
    # generated_at must be parseable ISO-8601
    datetime.fromisoformat(body["generated_at"])


def test_deep_health_returns_503_when_sqlite_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the SQLite probe blows up, the endpoint must return 503 so a
    K8s readiness probe pulls the pod out of rotation."""
    # Patch the supervisor's storage to raise on its lock context manager.
    sup = api_mod._SUPERVISOR
    assert sup is not None

    class _Boom:
        def __enter__(self):
            raise RuntimeError("simulated sqlite outage")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(sup.storage, "_lock", _Boom())

    r = client.get("/api/health/deep")
    assert r.status_code == 503
    body = json.loads(r.content)
    assert body["ok"] is False
    assert body["checks"]["sqlite_ok"] is False
    assert any("sqlite" in issue for issue in body["issues"])


def test_deep_health_returns_503_when_schema_outdated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the bundled max_known_version is bumped past the DB's user_version,
    the deep probe must flag a schema-outdated issue and return 503."""
    from catchem import migrations

    real_max = migrations.max_known_version()
    monkeypatch.setattr(migrations, "max_known_version", lambda: real_max + 99)

    r = client.get("/api/health/deep")
    assert r.status_code == 503
    body = json.loads(r.content)
    assert body["ok"] is False
    assert body["checks"]["schema_ok"] is False
    assert any("schema_outdated" in issue for issue in body["issues"])


def test_deep_health_news_poller_stale_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the poller is enabled and its last_run_at is older than
    ``5 x interval_seconds``, the deep probe must flag it stale + 503."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "true")
    # Floor at 10s is enforced by NewsPoller — pick the smallest legal value
    # so a "stale" timestamp is easy to produce.
    monkeypatch.setenv("CATCHEM_NEWS__POLL_INTERVAL_SECONDS", "10")
    reload_settings()

    class _FakePoller:
        interval_seconds = 10.0
        last_run_at = None
        started = False
        stopped = False

        def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    fake_poller = _FakePoller()
    monkeypatch.setattr(api_mod, "_build_news_poller", lambda *_args, **_kwargs: fake_poller)

    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    try:
        # Move the poller's last_run_at backwards by > 5 x interval.
        poller = api_mod._NEWS_POLLER
        assert poller is not None, "poller must be active for this test"
        poller.last_run_at = datetime.now(UTC) - timedelta(seconds=10 * 6)

        r = c.get("/api/health/deep")
        assert r.status_code == 503, r.text
        body = json.loads(r.content)
        assert body["ok"] is False
        assert body["checks"]["news_poller_ok"] is False
        assert body["checks"]["news_poller_enabled"] is True
        assert any("news_poller_stale" in issue for issue in body["issues"])
    finally:
        c.__exit__(None, None, None)


def test_simple_healthz_still_returns_minimal_status(client: TestClient) -> None:
    """The liveness probe at /healthz MUST stay simple — adding the deep
    probe should not have rewritten the cheap path."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_requires_matching_boot_token_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Tauri shell appends a boot token to /healthz so it cannot
    mistake an unrelated listener on the same port for the launched
    sidecar. When the token is configured, missing or mismatched tokens
    must be rejected."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_BOOT_TOKEN", "abc123")
    reload_settings()
    app = create_app(load_settings())
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 403
        assert c.get("/healthz?boot_token=wrong").status_code == 403
        r = c.get("/healthz?boot_token=abc123")
        assert r.json() == {"status": "ok"}
        assert r.headers["x-catchem-boot-token"] == "abc123"


def test_healthz_exposes_boot_token_header_for_tauri_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop webview must be able to read the echoed boot token
    from a cross-origin fetch response; otherwise the boot shim can never
    confirm that the /healthz response came from the launched sidecar."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_BOOT_TOKEN", "abc123")
    reload_settings()
    app = create_app(load_settings())
    with TestClient(app) as c:
        r = c.get("/healthz?boot_token=abc123", headers={"Origin": "tauri://localhost"})
        assert r.status_code == 200
        assert r.headers["x-catchem-boot-token"] == "abc123"
        exposed = r.headers.get("access-control-expose-headers", "")
        assert "X-Catchem-Boot-Token" in exposed
