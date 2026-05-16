"""List route returns COMPACT summary records — no body text leakage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_stack.api import create_app
from fusion_stack.settings import load_settings, reload_settings


@pytest.fixture
def populated_client(tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Seed 3 captures and yield a TestClient with the API live."""
    cap1 = synth_capture(capture_id="c-rates", doc_id="d-rates")
    cap2 = synth_capture(
        capture_id="c-aapl",
        doc_id="d-aapl",
        title="Apple beats earnings",
        text="Apple beat consensus. $AAPL up.",
        domain="wsj.com",
    )
    cap3 = synth_capture(capture_id="c-sport", doc_id="d-sport", domain="espn.com",
                         title="Local team wins championship",
                         text="The scoreboard tells the story; last-minute goal seals the trophy.")
    write_jsonl([json.loads(c.model_dump_json()) for c in (cap1, cap2, cap3)])
    monkeypatch.setenv("FUSION_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    reload_settings()
    app = create_app(load_settings())
    client = TestClient(app)
    client.__enter__()
    client.post("/replay", json={"max_records": 50})
    yield client
    client.__exit__(None, None, None)


REQUIRED_SUMMARY_KEYS = {
    "capture_id", "doc_id", "title", "domain", "language", "url",
    "is_finance_relevant", "finance_relevance_score",
    "asset_classes", "impact_reason_codes", "candidate_symbols",
    "sentiment_label", "sentiment_score",
    "evidence_preview", "evidence_count",
    "diagnostic_multimodal_enabled",
    "published_ts", "created_at",
}

# These fields belong to the DETAIL contract — they must not leak into list views.
FORBIDDEN_IN_SUMMARY = {
    "text_excerpt",
    "evidence_sentences",         # use evidence_preview + evidence_count
    "candidate_entities",
    "impact_horizons",
    "reason_text",
    "component_scores",
    "diagnostic_multimodal_result",
    "model_versions",
    "processing_mode",
}


@pytest.mark.parametrize("path", [
    "/records/by-asset-class/rates",
    "/records/by-reason/central_bank",
    "/records/by-symbol/AAPL",
    "/recent",
])
def test_list_routes_return_summary_shape(populated_client: TestClient, path: str) -> None:
    r = populated_client.get(path)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    for item in items:
        present = set(item.keys())
        missing = REQUIRED_SUMMARY_KEYS - present
        assert not missing, f"{path}: missing summary keys {missing}"
        leaked = present & FORBIDDEN_IN_SUMMARY
        assert not leaked, f"{path}: detail fields leaked into summary: {leaked}"


def test_summary_evidence_preview_is_truncated(populated_client: TestClient) -> None:
    r = populated_client.get("/recent?relevant_only=false")
    assert r.status_code == 200
    for item in r.json()["items"]:
        if item["evidence_preview"] is not None:
            assert len(item["evidence_preview"]) <= 240


def test_summary_excludes_full_text_payload(populated_client: TestClient) -> None:
    """No item in any list should expose the multi-KB text excerpt."""
    r = populated_client.get("/recent?relevant_only=false")
    items = r.json()["items"]
    serialized = json.dumps(items)
    # The synthetic text contains 'Powell said'. If text_excerpt slipped in we'd see it.
    assert "Powell said" not in serialized, "list payload leaked full body text"


def test_diagnostic_flag_present_but_pinned_false_in_production_safe(populated_client: TestClient) -> None:
    r = populated_client.get("/recent?relevant_only=false")
    for item in r.json()["items"]:
        # field present (so the UI doesn't have to handle "undefined") and pinned false
        assert "diagnostic_multimodal_enabled" in item
        assert item["diagnostic_multimodal_enabled"] is False
