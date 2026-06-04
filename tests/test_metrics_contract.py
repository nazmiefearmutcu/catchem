"""Stable JSON contract for /metrics and /ui/summary.

Downstream consumers (CLI status, dashboards, monitoring scripts) read these.
Document and pin the keys so they cannot silently disappear.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings

REQUIRED_METRICS_KEYS = {
    "mode",
    "diagnostic_enabled",
    "use_ml_stubs",
    "records",                # {"total": int, "finance_relevant": int}
    "dlq",
    "model_versions",
    "generated_at",
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_metrics_returns_stable_keys(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    missing = REQUIRED_METRICS_KEYS - set(body.keys())
    assert not missing, f"metrics missing keys: {missing}"
    assert isinstance(body["records"], dict)
    assert "total" in body["records"] and "finance_relevant" in body["records"]
    # Production safe default in tests → diagnostic always False
    assert body["diagnostic_enabled"] is False
    # generated_at is ISO 8601
    datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00"))


def test_ui_summary_contract_keys(client: TestClient) -> None:
    r = client.get("/ui/summary")
    assert r.status_code == 200
    body = r.json()
    required = {
        "mode", "is_production_safe", "diagnostic_allowed", "use_ml_stubs",
        "totals", "diagnostic_count",
        "asset_class_distribution", "reason_code_distribution", "sentiment_distribution",
        "recent_top", "dlq", "model_versions", "guards", "generated_at",
    }
    missing = required - set(body.keys())
    assert not missing, f"ui summary missing keys: {missing}"


def test_metrics_diagnostic_always_false_in_production_safe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    reload_settings()
    r = client.get("/metrics")
    assert r.json()["diagnostic_enabled"] is False


def test_metrics_records_counts_are_ints(client: TestClient) -> None:
    r = client.get("/metrics")
    body = r.json()
    assert isinstance(body["records"]["total"], int)
    assert isinstance(body["records"]["finance_relevant"], int)
    assert isinstance(body["dlq"], int)
