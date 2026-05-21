"""Pydantic response models for the API surface.

These models pin the wire contracts so summary endpoints stay COMPACT and
detail endpoints stay RICH. FastAPI uses them via `response_model=` to:
  * filter the dict shape to documented keys (drops accidental extras),
  * generate the OpenAPI schema,
  * give us a single place to evolve the contract.

Design rules:
  * Summary list payloads MUST NOT include `text_excerpt`. Listing a hundred
    rows would otherwise carry many hundreds of KB of body text.
  * Detail payloads carry the rich fields (text_excerpt, component_scores,
    evidence_sentences, candidate_entities, model_versions, processing_mode).
  * `extra="ignore"` so adding a field server-side never breaks old clients.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _CompactBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FinancialImpactSummary(_CompactBase):
    """Compact view for list/feed endpoints. NO text_excerpt."""

    capture_id: str
    doc_id: str
    title: str | None = None
    domain: str | None = None
    language: str | None = None
    url: str | None = None
    is_finance_relevant: bool
    finance_relevance_score: float
    asset_classes: list[str] = []
    impact_reason_codes: list[str] = []
    candidate_symbols: list[str] = []
    sentiment_label: str | None = None
    sentiment_score: float | None = None
    evidence_preview: str | None = None    # first evidence sentence, if any
    evidence_count: int = 0
    diagnostic_multimodal_enabled: bool = False
    published_ts: str | None = None
    created_at: str

    @classmethod
    def from_record_dict(cls, r: dict[str, Any]) -> "FinancialImpactSummary":
        ev = r.get("evidence_sentences") or []
        return cls(
            capture_id=r["capture_id"],
            doc_id=r["doc_id"],
            title=r.get("title"),
            domain=r.get("domain"),
            language=r.get("language"),
            url=r.get("url"),
            is_finance_relevant=bool(r.get("is_finance_relevant")),
            finance_relevance_score=float(r.get("finance_relevance_score", 0.0)),
            asset_classes=list(r.get("asset_classes", [])),
            impact_reason_codes=list(r.get("impact_reason_codes", [])),
            candidate_symbols=list(r.get("candidate_symbols", [])),
            sentiment_label=r.get("sentiment_label"),
            sentiment_score=r.get("sentiment_score"),
            evidence_preview=(ev[0][:240] if ev else None),
            evidence_count=len(ev),
            diagnostic_multimodal_enabled=bool(r.get("diagnostic_multimodal_enabled", False)),
            published_ts=r.get("published_ts"),
            created_at=str(r.get("created_at") or datetime.utcnow().isoformat()),
        )


class FinancialImpactDetail(_CompactBase):
    """Full record for the detail drawer. Includes all summary fields."""

    capture_id: str
    doc_id: str
    title: str | None = None
    text_excerpt: str = ""
    domain: str | None = None
    language: str | None = None
    url: str | None = None
    is_finance_relevant: bool
    finance_relevance_score: float
    asset_classes: list[str] = []
    impact_reason_codes: list[str] = []
    candidate_symbols: list[str] = []
    candidate_entities: list[str] = []
    impact_horizons: list[str] = []
    sentiment_label: str | None = None
    sentiment_score: float | None = None
    evidence_sentences: list[str] = []
    reason_text: str | None = None
    component_scores: dict[str, float] = {}
    diagnostic_multimodal_enabled: bool = False
    diagnostic_multimodal_result: dict[str, Any] | None = None
    processing_mode: str
    model_versions: dict[str, str] = {}
    published_ts: str | None = None
    created_at: str


class MetricsSummary(_CompactBase):
    """Stable contract for /metrics. Documented keys for CLI + UI consumers."""

    mode: str
    diagnostic_enabled: bool
    use_ml_stubs: bool
    records: dict[str, int]
    dlq: int
    model_versions: dict[str, str] = {}
    generated_at: str


class GuardSummary(_CompactBase):
    """Safe projection of GuardSnapshot. Never includes local paths."""

    ok: bool
    release_gate_passed: bool | None = None
    quarantine_state: str | None = None
    fusion_verdict_class: str | None = None
    safe_to_publish: bool | None = None
    safe_to_promote: bool | None = None
    governance_index_sha256: str | None = None
    error_code: str | None = None


class RecordListResponse(_CompactBase):
    items: list[FinancialImpactSummary]


# ── Catchem desktop additions ──────────────────────────────────────────────

class DemoRunResponse(_CompactBase):
    """Return shape for /ui/demo/paste and /ui/demo/upload.

    `jsonl_path` is exposed as a basename only (no absolute path leakage to UI).
    """

    capture_id: str
    jsonl_basename: str
    processed: int
    skipped: int
    record: FinancialImpactDetail


class AppInfoResponse(_CompactBase):
    """Catchem app banner / status surface."""

    name: str = "catchem"
    version: str
    commit_sha: str | None = None
    branch: str | None = None
    mode: str
    use_ml_stubs: bool
    diagnostic_allowed: bool
    static_bundle_present: bool
    model_versions: dict[str, str] = {}
    generated_at: str


class SidecarStatusResponse(_CompactBase):
    """Process self-report. The Tauri shell asks for this on every poll."""

    healthy: bool
    api_host: str
    api_port: int
    pid: int
    uptime_seconds: float
    records: dict[str, int]
    dlq: int
    diagnostic_enabled: bool
    generated_at: str


class LogTailResponse(_CompactBase):
    lines: list[str]
    truncated: bool


class MarketQuote(_CompactBase):
    """Local market quote contract.

    Fixture-backed quotes are explicitly stale; this model is a UI-safe shape,
    not a live-price promise.
    """

    symbol: str
    provider: str
    as_of: str | None = None
    retrieved_at: str
    currency: str | None = None
    last: float | None = None
    prev_close: float | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    market_state: str
    stale_after: str | None = None
    freshness_status: str
    error_code: str | None = None


class MarketQuoteBatchResponse(_CompactBase):
    items: list[MarketQuote]
    provider: str
    generated_at: str


__all__ = [
    "FinancialImpactSummary",
    "FinancialImpactDetail",
    "MetricsSummary",
    "GuardSummary",
    "RecordListResponse",
    "DemoRunResponse",
    "AppInfoResponse",
    "SidecarStatusResponse",
    "LogTailResponse",
    "MarketQuote",
    "MarketQuoteBatchResponse",
]
