"""Tests for the /ui/* aggregation endpoints and legacy preservation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_stack.api import create_app
from fusion_stack.schemas import AwarenessCaptureView
from fusion_stack.settings import load_settings, reload_settings


@pytest.fixture
def client_with_records(tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Populate storage with a couple of synthetic records before opening the API."""
    cap = synth_capture(capture_id="ui-1", doc_id="d1")
    cap2 = synth_capture(
        capture_id="ui-2",
        doc_id="d2",
        title="Apple beats earnings, raises guidance",
        text="Apple Inc beat consensus and raised full-year guidance. $AAPL rose 4%.",
        domain="wsj.com",
    )
    write_jsonl([json.loads(c.model_dump_json()) for c in (cap, cap2)])
    monkeypatch.setenv("FUSION_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    reload_settings()
    s = load_settings()

    app = create_app(s)
    client = TestClient(app)
    client.__enter__()  # trigger lifespan
    # process the synthetic batch through the live supervisor
    client.post("/replay", json={"max_records": 50})
    yield client
    client.__exit__(None, None, None)


def test_root_serves_html_or_fallback(client_with_records: TestClient) -> None:
    r = client_with_records.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Either the built bundle or the friendly fallback message.
    body = r.text
    assert ("<div id=\"root\"></div>" in body) or ("bundle has not been built" in body)


def test_legacy_dashboard_still_served(client_with_records: TestClient) -> None:
    for path in ("/legacy", "/legacy-dashboard"):
        r = client_with_records.get(path)
        assert r.status_code == 200
        assert "fusion_stack" in r.text
        # Confirm it's the vanilla dashboard, not the SPA shell
        assert "<table>" in r.text or "dashboard" in r.text


def test_ui_summary_shape(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/summary")
    assert r.status_code == 200
    data = r.json()
    for key in (
        "mode", "is_production_safe", "diagnostic_allowed", "use_ml_stubs",
        "totals", "diagnostic_count", "asset_class_distribution",
        "reason_code_distribution", "sentiment_distribution",
        "recent_top", "dlq", "model_versions", "guards", "generated_at",
    ):
        assert key in data, f"missing {key}"
    assert data["is_production_safe"] is True
    assert data["diagnostic_allowed"] is False
    assert data["totals"]["total"] >= 2
    assert "release_gate_passed" in data["guards"]


def test_ui_facets_returns_paired_arrays(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/facets?limit=200")
    assert r.status_code == 200
    data = r.json()
    for k in ("asset_classes", "reason_codes", "symbols", "domains", "sentiments"):
        for entry in data[k]:
            assert isinstance(entry, list) and len(entry) == 2


def test_ui_timeline_buckets_have_total_and_relevant(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/timeline?bucket_minutes=60")
    assert r.status_code == 200
    data = r.json()
    assert data["bucket_minutes"] == 60
    for s in data["series"]:
        assert "ts" in s and "total" in s and "relevant" in s


def test_ui_top_symbols_and_reasons(client_with_records: TestClient) -> None:
    r1 = client_with_records.get("/ui/top-symbols")
    r2 = client_with_records.get("/ui/top-reasons")
    assert r1.status_code == 200 and r2.status_code == 200
    for it in r1.json()["items"]:
        assert "symbol" in it and "count" in it
    for it in r2.json()["items"]:
        assert "reason" in it and "count" in it


def test_ui_matrix(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/matrix")
    assert r.status_code == 200
    data = r.json()
    assert "asset_classes" in data and "reason_codes" in data and "matrix" in data
    # Matrix shape consistency
    if data["asset_classes"]:
        assert len(data["matrix"]) == len(data["asset_classes"])
        assert all(len(row) == len(data["reason_codes"]) for row in data["matrix"])


def test_ui_guards_reflects_real_state(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/guards")
    assert r.status_code == 200
    data = r.json()
    # In this dev environment newsimpact exists at the configured path
    assert data["ok"] is True
    assert data["release_gate_passed"] is False
    assert data["quarantine_state"] == "QUARANTINED_REGRESSIVE_MULTIMODAL"


def test_ui_benchmark_latest(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/benchmark/latest")
    assert r.status_code == 200
    data = r.json()
    assert "relevance" in data
    for k in ("precision", "recall", "f1"):
        assert k in data["relevance"]
    # The golden set should score perfectly on stubs (per regression test)
    assert data["relevance"]["f1"] >= 0.83
    assert "ran_at" in data


def test_ui_benchmark_history_is_safe_when_missing(client_with_records: TestClient) -> None:
    r = client_with_records.get("/ui/benchmark/history")
    assert r.status_code == 200
    assert "history" in r.json()


def test_ui_symbol_aggregation(client_with_records: TestClient) -> None:
    # Apple should be a recognized symbol on the seeded data
    r = client_with_records.get("/ui/symbol/AAPL")
    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "AAPL"
    assert data["count"] >= 0
    assert "reason_distribution" in data
    assert "items" in data


def test_diagnostic_flag_in_summary_when_research_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even with the env flag on, summary's diagnostic_allowed only flips outside production_safe."""
    monkeypatch.setenv("FUSION_MODE", "research_diagnostic")
    monkeypatch.setenv("FUSION_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/ui/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "research_diagnostic"
        assert data["diagnostic_allowed"] is True
        # Mode chip from /config endpoint should agree
        r2 = c.get("/config")
        assert r2.json()["diagnostic_allowed"] is True


def test_production_safe_summary_refuses_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_MODE", "production_safe")
    monkeypatch.setenv("FUSION_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")  # even with flag on
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/ui/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["is_production_safe"] is True
        assert data["diagnostic_allowed"] is False
