"""Production-safe mode guarantees: diagnostic fields and guard internals
never reach a client even when the underlying storage contains truthy values.

This is defense in depth on top of `service.py`, which already emits
diagnostic_multimodal_* as False/None in production_safe. Even if a future
bug, a tampered DB row, or a research-mode artifact were to carry truthy
diagnostic data, the API surface MUST scrub it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.redaction import (
    PRODUCTION_SAFE_DIAGNOSTIC_FIELDS,
    SAFE_GUARD_KEYS,
    redact_record_for_mode,
    redact_records_for_mode,
    safe_guard_view,
)
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.settings import load_settings, reload_settings
from catchem.storage import Storage


def _record_with_diagnostic_truthy(capture_id: str = "diag-leak") -> FinancialImpactRecord:
    """Construct a record where diagnostic data is truthy — simulating a
    research-mode artifact that should NEVER leak through prod-safe routes."""
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title="Test record",
        text_excerpt="x",
        url="https://example.com/1",
        domain="example.com",
        is_finance_relevant=True,
        finance_relevance_score=0.7,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["AAPL"],
        candidate_entities=["Apple"],
        impact_horizons=["one_day"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.8,
        evidence_sentences=["Apple beats earnings"],
        reason_text="equities | earnings",
        component_scores={"asset_class_max": 0.8},
        diagnostic_multimodal_enabled=True,                                     # truthy on purpose
        diagnostic_multimodal_result={"label": "should_be_scrubbed_in_prod"},   # truthy on purpose
        processing_mode=ProcessingMode.RESEARCH_DIAGNOSTIC,
        model_versions={"zero_shot": "stub-zero-shot/v1"},
        created_at=datetime.now(timezone.utc),
    )


# ── pure unit tests for the redactor ────────────────────────────────────────

def test_redact_record_strips_diagnostic_in_production_safe() -> None:
    rec = {"capture_id": "x", "diagnostic_multimodal_enabled": True,
           "diagnostic_multimodal_result": {"label": "leak"}}
    out = redact_record_for_mode(rec, production_safe=True)
    assert out is not None
    assert out["diagnostic_multimodal_enabled"] is False
    assert out["diagnostic_multimodal_result"] is None
    assert out["capture_id"] == "x"


def test_redact_record_passes_through_in_research_mode() -> None:
    rec = {"capture_id": "x", "diagnostic_multimodal_enabled": True,
           "diagnostic_multimodal_result": {"label": "ok"}}
    out = redact_record_for_mode(rec, production_safe=False)
    assert out is not None
    assert out["diagnostic_multimodal_enabled"] is True
    assert out["diagnostic_multimodal_result"] == {"label": "ok"}


def test_redact_records_list_handles_empty_and_none() -> None:
    assert redact_records_for_mode(None, production_safe=True) == []
    assert redact_records_for_mode([], production_safe=True) == []


def test_redact_record_does_not_mutate_input() -> None:
    rec = {"capture_id": "x", "diagnostic_multimodal_enabled": True}
    out = redact_record_for_mode(rec, production_safe=True)
    # input unchanged
    assert rec["diagnostic_multimodal_enabled"] is True
    # output scrubbed
    assert out is not None
    assert out["diagnostic_multimodal_enabled"] is False


def test_safe_guard_view_keeps_only_whitelisted_keys() -> None:
    snap = {
        "ok": True,
        "release_gate_passed": False,
        "quarantine_state": "Q",
        "fusion_verdict_class": "FR",
        "safe_to_publish": False,
        "safe_to_promote": False,
        "governance_index_sha256": "abc",
        "governance_index_path": "/absolute/path/that/should/not/leak",
        "secret_key": "shh",
    }
    out = safe_guard_view(snap)
    for k in SAFE_GUARD_KEYS:
        assert k in out
    assert "governance_index_path" not in out
    assert "secret_key" not in out


def test_safe_guard_view_classifies_errors_without_leaking() -> None:
    snap = {"ok": False, "error": "/Users/secret/path/governance_index.json missing at /Users/secret/path"}
    out = safe_guard_view(snap)
    assert out["ok"] is False
    assert out["error_code"] == "missing_governance_index"
    assert "error" not in out
    # No path leak
    assert "/Users" not in str(out)


# ── API-level tests ─────────────────────────────────────────────────────────

@pytest.fixture
def prod_safe_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Storage]:
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    reload_settings()
    s = load_settings()
    app = create_app(s)
    client = TestClient(app)
    client.__enter__()
    # Inject a truthy-diagnostic record straight into storage to bypass the
    # service layer's safety. The API surface must still scrub.
    sup = client.app.state if hasattr(client.app, "state") else None  # type: ignore[attr-defined]
    # We need the live supervisor; it was created by lifespan.
    from catchem import api as api_mod
    storage = api_mod._SUPERVISOR.storage  # type: ignore[union-attr]
    storage.insert_record(_record_with_diagnostic_truthy("diag-leak-1"))
    storage.insert_record(_record_with_diagnostic_truthy("diag-leak-2"))
    yield client, storage
    client.__exit__(None, None, None)


def test_recent_endpoint_scrubs_diagnostic_in_production_safe(prod_safe_app) -> None:
    """Summary payload exposes only `diagnostic_multimodal_enabled` (pinned False).
    The full `diagnostic_multimodal_result` is intentionally absent from summary
    contracts — see FinancialImpactSummary."""
    client, _ = prod_safe_app
    r = client.get("/recent?limit=10&relevant_only=false")
    assert r.status_code == 200
    for it in r.json()["items"]:
        assert it["diagnostic_multimodal_enabled"] is False, it
        # Detail-only field must NOT appear in the summary shape
        assert "diagnostic_multimodal_result" not in it, it


def test_record_detail_scrubs_diagnostic_in_production_safe(prod_safe_app) -> None:
    client, _ = prod_safe_app
    r = client.get("/record/diag-leak-1")
    assert r.status_code == 200
    body = r.json()
    assert body["diagnostic_multimodal_enabled"] is False
    assert body["diagnostic_multimodal_result"] is None


def test_by_symbol_scrubs_diagnostic(prod_safe_app) -> None:
    client, _ = prod_safe_app
    r = client.get("/records/by-symbol/AAPL")
    assert r.status_code == 200
    for it in r.json()["items"]:
        assert it["diagnostic_multimodal_enabled"] is False
        # Detail-only field must NOT appear in summary
        assert "diagnostic_multimodal_result" not in it


def test_ui_summary_scrubs_recent_top_and_diagnostic_count(prod_safe_app) -> None:
    client, _ = prod_safe_app
    r = client.get("/ui/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["is_production_safe"] is True
    assert body["diagnostic_allowed"] is False
    assert body["diagnostic_count"] == 0
    for it in body["recent_top"]:
        assert it["diagnostic_multimodal_enabled"] is False
        assert it["diagnostic_multimodal_result"] is None


def test_dashboard_endpoint_redacts_recent_in_production(prod_safe_app) -> None:
    client, _ = prod_safe_app
    r = client.get("/dashboard?limit=20")
    assert r.status_code == 200
    body = r.json()
    assert body["diagnostic_count"] == 0
    for it in body["recent"]:
        assert it["diagnostic_multimodal_enabled"] is False
        assert it["diagnostic_multimodal_result"] is None


def test_ui_guards_endpoint_does_not_leak_path(prod_safe_app) -> None:
    client, _ = prod_safe_app
    r = client.get("/ui/guards")
    assert r.status_code == 200
    body = r.json()
    assert "governance_index_path" not in body
    # No absolute path should appear in any string field
    for v in body.values():
        if isinstance(v, str):
            assert "/Users/" not in v
            assert "/etc/" not in v


def test_ui_guards_failure_contract_uses_error_code_not_error(prod_safe_app) -> None:
    client, _ = prod_safe_app
    with patch(
        "catchem.api._guard_snapshot",
        return_value={"ok": False, "error": "/Users/secret/governance_index.json missing"},
    ):
        r = client.get("/ui/guards")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error_code"] == "missing_governance_index"
    assert "error" not in body
    assert "/Users" not in str(body)


def test_metrics_diagnostic_pinned_false_in_production_safe(prod_safe_app) -> None:
    client, _ = prod_safe_app
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["diagnostic_enabled"] is False
    assert "generated_at" in body


def test_diagnostic_env_flag_cannot_force_diagnostic_in_production_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even setting CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED=true does not
    enable diagnostic when mode is production_safe."""
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    reload_settings()
    s = load_settings()
    assert s.diagnostic_allowed() is False

    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/ui/summary")
        assert r.json()["diagnostic_allowed"] is False
        # And /config agrees
        cfg = c.get("/config").json()
        assert cfg["diagnostic_allowed"] is False


def test_service_in_production_safe_does_not_construct_diagnostic_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The diagnostic adapter must not even be constructed in production_safe.
    We patch it to raise on construction and confirm the service still works.
    """
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    reload_settings()
    s = load_settings()

    with patch("catchem.service.NewsImpactGuardedAdapter") as patched:
        patched.side_effect = AssertionError("must not be constructed in production_safe")
        from catchem.service import build_service
        svc = build_service(s)
        assert svc.diagnostic_enabled is False
        # The adapter must not have been called
        patched.assert_not_called()
