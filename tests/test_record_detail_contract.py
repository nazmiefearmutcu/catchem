"""Detail route returns the rich record shape including evidence, scores,
component_scores, model_versions, processing_mode, and provenance fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_stack.api import create_app
from fusion_stack.settings import load_settings, reload_settings


@pytest.fixture
def populated_client(tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    cap = synth_capture(capture_id="d-fed", doc_id="d-fed-doc")
    write_jsonl([json.loads(cap.model_dump_json())])
    monkeypatch.setenv("FUSION_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    reload_settings()
    app = create_app(load_settings())
    client = TestClient(app)
    client.__enter__()
    client.post("/replay", json={"max_records": 5})
    yield client
    client.__exit__(None, None, None)


REQUIRED_DETAIL_KEYS = {
    "capture_id", "doc_id", "title", "domain", "language", "url",
    "is_finance_relevant", "finance_relevance_score",
    "asset_classes", "impact_reason_codes",
    "candidate_symbols", "candidate_entities", "impact_horizons",
    "sentiment_label", "sentiment_score",
    "evidence_sentences", "reason_text",
    "component_scores",
    "diagnostic_multimodal_enabled", "diagnostic_multimodal_result",
    "processing_mode", "model_versions",
    "published_ts", "created_at",
}


def test_detail_has_full_record_shape(populated_client: TestClient) -> None:
    r = populated_client.get("/record/d-fed")
    assert r.status_code == 200, r.text
    body = r.json()
    missing = REQUIRED_DETAIL_KEYS - set(body.keys())
    assert not missing, f"detail missing keys: {missing}"
    # The component_scores breakdown must be a dict with at least raw_relevance_score
    assert isinstance(body["component_scores"], dict)
    assert "raw_relevance_score" in body["component_scores"] or "asset_class_max" in body["component_scores"]
    # Model versions documented for provenance
    assert isinstance(body["model_versions"], dict)
    assert len(body["model_versions"]) > 0


def test_detail_404_for_unknown_capture(populated_client: TestClient) -> None:
    r = populated_client.get("/record/does-not-exist")
    assert r.status_code == 404
    assert "capture_not_found" in r.text


def test_detail_serializes_timestamps_as_iso_strings(populated_client: TestClient) -> None:
    r = populated_client.get("/record/d-fed")
    body = r.json()
    # created_at is always present; should be a string
    assert isinstance(body["created_at"], str) and "T" in body["created_at"]


def test_detail_diagnostic_is_false_in_production_safe(populated_client: TestClient) -> None:
    r = populated_client.get("/record/d-fed")
    body = r.json()
    assert body["diagnostic_multimodal_enabled"] is False
    assert body["diagnostic_multimodal_result"] is None
