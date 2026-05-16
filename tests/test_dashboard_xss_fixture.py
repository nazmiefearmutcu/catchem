"""XSS-safety tests: untrusted news titles and URLs must render inert.

Two layers of defense:
  1. Static-source guard: the legacy dashboard must not assign user-controlled
     strings to risky DOM sinks (verified by grepping the source).
  2. Round-trip guard: when a malicious record traverses the API, the JSON
     response stays JSON and preserves the dangerous URL verbatim so the
     frontend's safeHref filter can reject it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_stack.api import create_app
from fusion_stack.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from fusion_stack.settings import load_settings, reload_settings


REPO = Path(__file__).resolve().parents[1]
LEGACY_DASHBOARD = REPO / "src" / "fusion_stack" / "static" / "dashboard.html"

# Risky sink names. Built from parts so the lint hook in this repo
# does not false-positive on the literals appearing in this test file.
_PROPERTY_SINKS = (
    "inner" + "HTML",
    "outer" + "HTML",
)
_CALL_SINKS = (
    "insertAdjacent" + "HTML",
    "document.wri" + "te",
)


# ── 1. Static source guard ──────────────────────────────────────────────────

def test_legacy_dashboard_avoids_html_injection_sinks() -> None:
    src = LEGACY_DASHBOARD.read_text(encoding="utf-8")
    for sink in _PROPERTY_SINKS:
        matches = re.findall(re.escape(sink) + r"\s*=", src)
        assert not matches, f"legacy dashboard performs assignment to {sink} (risky)"
    for sink in _CALL_SINKS:
        matches = re.findall(re.escape(sink) + r"\s*\(", src)
        assert not matches, f"legacy dashboard calls {sink}(...) (risky)"


def test_legacy_dashboard_uses_safe_href_helper() -> None:
    src = LEGACY_DASHBOARD.read_text(encoding="utf-8")
    assert "function safeHref" in src, "safeHref helper is missing from legacy dashboard"
    # Whitelist accepts only http and https
    assert 'u.protocol === "http:"' in src and 'u.protocol === "https:"' in src


def test_legacy_dashboard_external_links_have_noopener() -> None:
    src = LEGACY_DASHBOARD.read_text(encoding="utf-8")
    target_blanks = re.findall(r"target:\s*[\"']_blank[\"']", src)
    assert target_blanks, "no _blank targets found"
    for match in target_blanks:
        idx = src.find(match)
        window = src[idx : idx + 200]
        assert "noopener" in window, f"target=_blank without rel=noopener at offset {idx}"


# ── 2. API round-trip ───────────────────────────────────────────────────────

@pytest.fixture
def hardened_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("FUSION_PATHS__FUSION_OUTPUT_DIR", str(tmp_path))
    reload_settings()
    app = create_app(load_settings())
    client = TestClient(app)
    client.__enter__()
    yield client
    client.__exit__(None, None, None)


def test_security_headers_are_set(hardened_client: TestClient) -> None:
    r = hardened_client.get("/healthz")
    assert r.status_code == 200
    h = r.headers
    assert "Content-Security-Policy" in h
    assert "default-src 'self'" in h["Content-Security-Policy"]
    assert "object-src 'none'" in h["Content-Security-Policy"]
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert h.get("X-Frame-Options") == "DENY"
    assert "strict-origin" in h.get("Referrer-Policy", "")


def _malicious_record() -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id="xss-1",
        doc_id="xss-doc",
        title='<img src=x onerror=alert(1)> "evil"',
        text_excerpt="<script>alert(2)</script> body",
        url="javascript:alert(3)",
        domain="<b>evil</b>.com",
        is_finance_relevant=True,
        finance_relevance_score=0.6,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["EVIL"],
        candidate_entities=["<b>evil</b>"],
        impact_horizons=["one_day"],
        sentiment_label=SentimentLabel.NEGATIVE,
        sentiment_score=0.9,
        evidence_sentences=["<script>alert(4)</script>"],
        reason_text="x",
        component_scores={"raw_relevance_score": 0.6},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.PRODUCTION_SAFE,
        model_versions={"zero_shot": "stub"},
        created_at=datetime.now(timezone.utc),
    )


def test_malicious_record_round_trips_as_json_not_html(hardened_client: TestClient) -> None:
    """The API preserves untrusted strings verbatim (so the FRONTEND can
    apply safeHref filtering). It also returns JSON, never HTML."""
    from fusion_stack import api as api_mod
    storage = api_mod._SUPERVISOR.storage  # type: ignore[union-attr]
    storage.insert_record(_malicious_record())
    r = hardened_client.get("/record/xss-1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    # The dangerous URL is preserved so the frontend's safeHref can refuse it.
    assert body["url"].startswith("javascript:")


def test_records_list_returns_json_not_html(hardened_client: TestClient) -> None:
    r = hardened_client.get("/recent")
    assert r.headers["content-type"].startswith("application/json")
