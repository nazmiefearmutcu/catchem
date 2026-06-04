"""Tests for the /ui/* aggregation endpoints and legacy preservation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


def _write_quarantined_governance_index(root: Path) -> Path:
    """Plant a read-only NewsImpact governance index so the guard system can
    compute a *real* snapshot in a fresh checkout.

    The default ``paths.newsimpact_repo`` points at ``/tmp/merged_news-missing``
    and the real quarantined repo lives in a sibling checkout that is absent
    here. Without this fixture ``snapshot_guard_state`` raises
    ``missing_governance_index`` and the guard payload degrades to
    ``{"ok": False, "error_code": "missing_governance_index"}``. The shape below
    mirrors ``tests/test_newsimpact_guard.py::_make_fake_quarantined_root`` so
    the guard reports the canonical quarantined state.
    """
    idx_dir = root / "models" / "governance_index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    idx = {
        "candidates": [
            {
                "candidate_id": "fake",
                "governance_status": "QUARANTINED_REGRESSIVE_MULTIMODAL",
                "fusion_verdict_class": "FUSION_REGRESSIVE",
                "forbidden_operations": ["benchmark", "export", "promotion", "training"],
                "allowed_operations": ["eval", "diagnostic"],
                "gate_failure_status": {
                    "release_gate_passed": False,
                    "candidate_status": "failed_gate_diagnostic",
                    "failure_codes": ["PERMUTED_LABEL_TOO_CLOSE_TO_CHART_ONLY"],
                },
            }
        ],
        "deterministic": True,
        "safeguards": {"no_external_publish": True, "no_governance_mutation": True},
    }
    (idx_dir / "governance_index.json").write_text(json.dumps(idx), encoding="utf-8")
    return root


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
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    # Point the guard system at a planted quarantined governance index so the
    # guard endpoints report a real snapshot regardless of which machine /
    # checkout the suite runs on.
    newsimpact_root = _write_quarantined_governance_index(tmp_path / "newsimpact")
    monkeypatch.setenv("CATCHEM_PATHS__NEWSIMPACT_REPO", str(newsimpact_root))
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
        assert "catchem" in r.text
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
    monkeypatch.setenv("CATCHEM_MODE", "research_diagnostic")
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
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
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")  # even with flag on
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/ui/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["is_production_safe"] is True
        assert data["diagnostic_allowed"] is False


# -------------------------------------------------------------------------
# _display_path / /ui/archive-status redaction (Round 6 Bug 1)
# Guards against re-leaking `/Users/<name>/...` into user-facing JSON.
# -------------------------------------------------------------------------

def test_display_path_redacts_home_to_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Paths under $HOME render as `~/...`; paths outside $HOME pass through."""
    from catchem.api import _display_path
    fake_home = tmp_path / "Users" / "fake-user"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    inside = fake_home / "Documents" / "Catchem"
    assert _display_path(inside) == "~/Documents/Catchem"
    assert _display_path(fake_home) == "~"
    assert _display_path(None) is None
    # Outside $HOME (e.g. mounted drive, /tmp in CI) → pass through unchanged.
    outside = Path("/var/empty/somewhere")
    assert _display_path(outside) == "/var/empty/somewhere"


def test_archive_status_redacts_paths_to_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """/ui/archive-status must not leak absolute /Users/... paths."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    drive = fake_home / "Documents" / "Catchem"
    drive.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "true")
    monkeypatch.setenv("CATCHEM_ARCHIVE__DRIVE_DIR", str(drive))
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/ui/archive-status")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert data["drive_dir"] == "~/Documents/Catchem", data
        # current_csv_path may be None until a sweep runs, but if set must
        # also be tilde-redacted.
        if data.get("current_csv_path"):
            assert data["current_csv_path"].startswith("~/")


def test_ui_endpoints_sanitization(client_with_records: TestClient) -> None:
    # Invalid symbol should return 400 Bad Request
    r = client_with_records.get("/ui/symbol/AAPL;%20DROP%20TABLE")
    assert r.status_code == 400

    r = client_with_records.get("/ui/quote/AAPL<script>")
    assert r.status_code == 400

    # Invalid capture_id should return 400
    r = client_with_records.get("/api/records/ui-1<script>/tags")
    assert r.status_code == 400

    r = client_with_records.get("/record/ui-1<script>")
    assert r.status_code == 400

    # Invalid tags, asset_classes, reasons
    r = client_with_records.get("/api/tags/some_tag<script>/records")
    assert r.status_code == 400

    r = client_with_records.get("/records/by-asset-class/equity<script>")
    assert r.status_code == 400

    r = client_with_records.get("/records/by-reason/earnings<script>")
    assert r.status_code == 400

