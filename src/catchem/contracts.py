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

from datetime import UTC, datetime
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
    def from_record_dict(cls, r: dict[str, Any]) -> FinancialImpactSummary:
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
            created_at=str(r.get("created_at") or datetime.now(UTC).isoformat()),
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
    # Monotonic total line count of the underlying file (not just the returned
    # tail). The UI's lines/min rate KPI must diff a value that keeps growing;
    # diffing len(lines) plateaus at the cap once the file exceeds it and the
    # rate reads 0 forever. Defaults to len(lines) for back-compat.
    total_lines: int = 0


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


class RelevanceMetric(_CompactBase):
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None


class BenchmarkItemDetails(_CompactBase):
    capture_id: str
    expected_finance_relevant: bool
    predicted_finance_relevant: bool
    score: float
    expected_asset_classes: list[str] = []
    predicted_asset_classes: list[str] = []
    expected_reason_codes: list[str] = []
    predicted_reason_codes: list[str] = []


class UiBenchmarkLatestResponse(_CompactBase):
    schema_version: int
    dataset_name: str
    generated_at: str
    ran_at: str
    relevance: RelevanceMetric
    asset_class_f1: dict[str, float] = {}
    reason_code_f1: dict[str, float] = {}
    symbol_recall: float | None = None
    sentiment_accuracy: float | None = None
    n: int
    per_item: list[BenchmarkItemDetails] = []


class BenchmarkHistoryItem(_CompactBase):
    schema_version: int
    dataset_name: str
    generated_at: str
    relevance: RelevanceMetric
    asset_class_f1: dict[str, float] = {}
    reason_code_f1: dict[str, float] = {}
    symbol_recall: float | None = None
    sentiment_accuracy: float | None = None
    n: int
    per_item: list[BenchmarkItemDetails] = []


class UiBenchmarkHistoryResponse(_CompactBase):
    history: list[BenchmarkHistoryItem] = []


class UiSummaryResponse(_CompactBase):
    mode: str
    is_production_safe: bool
    diagnostic_allowed: bool
    use_ml_stubs: bool
    totals: dict[str, int]
    diagnostic_count: int
    asset_class_distribution: dict[str, int] = {}
    reason_code_distribution: dict[str, int] = {}
    sentiment_distribution: dict[str, int] = {}
    recent_top: list[FinancialImpactDetail]
    dlq: int
    model_versions: dict[str, str] = {}
    guards: GuardSummary
    generated_at: str


class UiFacetsResponse(_CompactBase):
    window_total: int
    window_relevant: int
    asset_classes: list[tuple[str, int]] = []
    reason_codes: list[tuple[str, int]] = []
    symbols: list[tuple[str, int]] = []
    domains: list[tuple[str, int]] = []
    sentiments: list[tuple[str, int]] = []


class TimelineSeriesEntry(_CompactBase):
    ts: str
    total: int
    relevant: int


class UiTimelineResponse(_CompactBase):
    bucket_minutes: int
    series: list[TimelineSeriesEntry]


class TopSymbolEntry(_CompactBase):
    symbol: str
    count: int


class UiTopSymbolsResponse(_CompactBase):
    items: list[TopSymbolEntry]


class TopReasonEntry(_CompactBase):
    reason: str
    count: int


class UiTopReasonsResponse(_CompactBase):
    items: list[TopReasonEntry]


class UiTrendsResponse(_CompactBase):
    buckets: list[str]
    asset_classes: list[str]
    series: dict[str, list[int]]


class UiMatrixResponse(_CompactBase):
    asset_classes: list[str]
    reason_codes: list[str]
    matrix: list[list[int]]


class UiSymbolDetailResponse(_CompactBase):
    symbol: str
    count: int
    reason_distribution: dict[str, int] = {}
    sentiment_distribution: dict[str, int] = {}
    items: list[FinancialImpactSummary]


class NewsFeedHealthEntry(_CompactBase):
    name: str
    url: str
    fallback_domain: str | None = None
    ok: bool
    backed_off: bool
    status_code: int | None = None
    error: str | None = None
    item_count: int = 0
    items_total: int = 0
    last_fetch_at: str | None = None
    elapsed_ms: float = 0.0
    total_fetches: int = 0
    total_errors: int = 0
    consecutive_errors: int = 0
    cooldown_until: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    consecutive_empty: int = 0
    adaptive_cadence: int = 1
    total_new_items: int = 0


class UiNewsStatusResponse(_CompactBase):
    enabled: bool
    feeds: int
    interval_seconds: int | None = None
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_ingested: int = 0
    total_ingested: int = 0
    last_error: str | None = None
    is_polling: bool
    last_new_at: str | None = None
    empty_ticks: int = 0
    last_avg_publisher_lag_seconds: float | None = None
    last_median_publisher_lag_seconds: float | None = None
    unhealthy_feeds: int = 0
    backed_off_feeds: int = 0
    feed_health: list[NewsFeedHealthEntry] = []
    max_item_age_seconds: int | None = None
    last_stale_skipped: int = 0


class UiNewsPollNowResponse(_CompactBase):
    ingested: int
    total_ingested: int


class UiArchiveStatusResponse(_CompactBase):
    enabled: bool
    drive_dir: str | None = None
    interval_seconds: int | None = None
    local_cap_rows: int | None = None
    last_run_at: str | None = None
    last_archived_count: int = 0
    total_archived: int = 0
    last_error: str | None = None
    is_archiving: bool
    current_csv_path: str | None = None


class UiArchiveNowResponse(_CompactBase):
    archived: int
    csv_path: str | None = None
    error: str | None = None
    total_archived: int


__all__ = [
    "AppInfoResponse",
    "BenchmarkHistoryItem",
    "BenchmarkItemDetails",
    "DemoRunResponse",
    "FinancialImpactDetail",
    "FinancialImpactSummary",
    "GuardSummary",
    "LogTailResponse",
    "MarketQuote",
    "MarketQuoteBatchResponse",
    "MetricsSummary",
    "NewsFeedHealthEntry",
    "RecordListResponse",
    "RelevanceMetric",
    "SidecarStatusResponse",
    "TimelineSeriesEntry",
    "TopReasonEntry",
    "TopSymbolEntry",
    "UiArchiveNowResponse",
    "UiArchiveStatusResponse",
    "UiBenchmarkHistoryResponse",
    "UiBenchmarkLatestResponse",
    "UiFacetsResponse",
    "UiMatrixResponse",
    "UiNewsPollNowResponse",
    "UiNewsStatusResponse",
    "UiSummaryResponse",
    "UiSymbolDetailResponse",
    "UiTimelineResponse",
    "UiTopReasonsResponse",
    "UiTopSymbolsResponse",
    "UiTrendsResponse",
]
