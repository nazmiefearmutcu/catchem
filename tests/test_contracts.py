"""Dedicated unit tests for catchem.contracts (API wire models).

These pydantic models pin the HTTP contract: summary payloads stay COMPACT
(no text_excerpt), detail payloads stay RICH, and every model uses
extra="ignore" so a server-side field addition never breaks an old client.

This file constructs valid + invalid instances of each model, exercises the
`FinancialImpactSummary.from_record_dict` mapper (including missing-key
fallbacks, evidence preview truncation, and the empty-evidence path), and
asserts the compact/rich split + serialization round-trips.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from catchem.contracts import (
    AppInfoResponse,
    DemoRunResponse,
    FinancialImpactDetail,
    FinancialImpactSummary,
    GuardSummary,
    LogTailResponse,
    MarketQuote,
    MarketQuoteBatchResponse,
    MetricsSummary,
    RecordListResponse,
    SidecarStatusResponse,
)


def _full_record_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "capture_id": "cap-1",
        "doc_id": "doc-1",
        "title": "Fed hikes rates",
        "text_excerpt": "The Federal Reserve raised rates by 25bps on Wednesday.",
        "domain": "reuters.com",
        "language": "en",
        "url": "https://reuters.com/article/1",
        "is_finance_relevant": True,
        "finance_relevance_score": 0.82,
        "asset_classes": ["rates", "equities"],
        "impact_reason_codes": ["monetary_policy"],
        "candidate_symbols": ["SPY"],
        "candidate_entities": ["Federal Reserve"],
        "impact_horizons": ["one_day"],
        "sentiment_label": "negative",
        "sentiment_score": -0.4,
        "evidence_sentences": ["Yields jumped on the news.", "Equities sold off."],
        "reason_text": "rates | monetary_policy",
        "component_scores": {"asset_class_max": 0.9},
        "diagnostic_multimodal_enabled": False,
        "diagnostic_multimodal_result": None,
        "processing_mode": "production_safe",
        "model_versions": {"zero_shot": "stub/v1"},
        "published_ts": "2026-05-16T10:00:00Z",
        "created_at": "2026-05-16T10:05:00Z",
    }
    base.update(overrides)
    return base


# ── FinancialImpactSummary.from_record_dict ─────────────────────────────────


def test_summary_from_full_record_dict() -> None:
    s = FinancialImpactSummary.from_record_dict(_full_record_dict())
    assert s.capture_id == "cap-1"
    assert s.doc_id == "doc-1"
    assert s.is_finance_relevant is True
    assert s.finance_relevance_score == 0.82
    assert s.asset_classes == ["rates", "equities"]
    # First evidence sentence becomes the preview; count reflects all.
    assert s.evidence_preview == "Yields jumped on the news."
    assert s.evidence_count == 2


def test_summary_never_carries_text_excerpt() -> None:
    # Compact contract: text_excerpt must not exist on the summary model at all.
    s = FinancialImpactSummary.from_record_dict(_full_record_dict())
    assert not hasattr(s, "text_excerpt")
    assert "text_excerpt" not in s.model_dump()


def test_summary_from_minimal_dict_uses_fallbacks() -> None:
    # Only the two truly-required keys present; everything else falls back.
    s = FinancialImpactSummary.from_record_dict({"capture_id": "c", "doc_id": "d"})
    assert s.title is None
    assert s.domain is None
    assert s.is_finance_relevant is False           # bool(None) → False
    assert s.finance_relevance_score == 0.0         # default 0.0
    assert s.asset_classes == []
    assert s.impact_reason_codes == []
    assert s.candidate_symbols == []
    assert s.evidence_preview is None               # empty-evidence path
    assert s.evidence_count == 0
    assert s.diagnostic_multimodal_enabled is False
    # created_at is synthesized (utcnow isoformat) when absent.
    assert isinstance(s.created_at, str) and "T" in s.created_at


def test_summary_evidence_preview_truncates_to_240_chars() -> None:
    long_sentence = "x" * 500
    s = FinancialImpactSummary.from_record_dict(
        _full_record_dict(evidence_sentences=[long_sentence])
    )
    assert s.evidence_preview is not None
    assert len(s.evidence_preview) == 240
    assert s.evidence_count == 1


def test_summary_handles_evidence_none_value() -> None:
    # `r.get("evidence_sentences") or []` → None coalesces to empty list.
    s = FinancialImpactSummary.from_record_dict(
        _full_record_dict(evidence_sentences=None)
    )
    assert s.evidence_preview is None
    assert s.evidence_count == 0


def test_summary_coerces_score_and_flag_types() -> None:
    s = FinancialImpactSummary.from_record_dict(
        {"capture_id": "c", "doc_id": "d", "is_finance_relevant": 1,
         "finance_relevance_score": "0.5", "diagnostic_multimodal_enabled": 1}
    )
    assert s.is_finance_relevant is True
    assert s.finance_relevance_score == 0.5
    assert s.diagnostic_multimodal_enabled is True


def test_summary_missing_capture_id_raises_keyerror() -> None:
    # from_record_dict indexes r["capture_id"] / r["doc_id"] directly.
    with pytest.raises(KeyError):
        FinancialImpactSummary.from_record_dict({"doc_id": "d"})


# ── FinancialImpactSummary: direct construction + extra=ignore ──────────────


def test_summary_ignores_unknown_fields() -> None:
    s = FinancialImpactSummary(
        capture_id="c", doc_id="d", is_finance_relevant=True,
        finance_relevance_score=0.1, created_at="2026-01-01T00:00:00Z",
        some_future_field="ignored",  # type: ignore[call-arg]
    )
    assert "some_future_field" not in s.model_dump()


def test_summary_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        FinancialImpactSummary(capture_id="c")  # type: ignore[call-arg]


# ── FinancialImpactDetail ────────────────────────────────────────────────────


def test_detail_carries_rich_fields() -> None:
    d = FinancialImpactDetail(**_full_record_dict())  # type: ignore[arg-type]
    assert d.text_excerpt.startswith("The Federal Reserve")
    assert d.candidate_entities == ["Federal Reserve"]
    assert d.impact_horizons == ["one_day"]
    assert d.component_scores == {"asset_class_max": 0.9}
    assert d.model_versions == {"zero_shot": "stub/v1"}
    assert d.processing_mode == "production_safe"


def test_detail_defaults_when_optional_absent() -> None:
    d = FinancialImpactDetail(
        capture_id="c", doc_id="d", is_finance_relevant=False,
        finance_relevance_score=0.0, processing_mode="production_safe",
        created_at="2026-01-01T00:00:00Z",
    )
    assert d.text_excerpt == ""
    assert d.evidence_sentences == []
    assert d.component_scores == {}
    assert d.diagnostic_multimodal_result is None


def test_detail_ignores_extra_fields() -> None:
    d = FinancialImpactDetail(**_full_record_dict(orphan_key="drop me"))  # type: ignore[arg-type]
    assert "orphan_key" not in d.model_dump()


def test_detail_round_trip() -> None:
    d = FinancialImpactDetail(**_full_record_dict())  # type: ignore[arg-type]
    restored = FinancialImpactDetail.model_validate_json(d.model_dump_json())
    assert restored == d


# ── MetricsSummary ───────────────────────────────────────────────────────────


def test_metrics_summary_valid_and_defaults() -> None:
    m = MetricsSummary(
        mode="production_safe", diagnostic_enabled=False, use_ml_stubs=True,
        records={"total": 10, "finance_relevant": 4}, dlq=0,
        generated_at="2026-05-16T10:00:00Z",
    )
    assert m.records["total"] == 10
    assert m.model_versions == {}  # default


def test_metrics_summary_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        MetricsSummary(mode="x", diagnostic_enabled=False)  # type: ignore[call-arg]


# ── GuardSummary ─────────────────────────────────────────────────────────────


def test_guard_summary_minimal_and_optionals_none() -> None:
    g = GuardSummary(ok=True)
    assert g.ok is True
    assert g.release_gate_passed is None
    assert g.error_code is None


def test_guard_summary_full() -> None:
    g = GuardSummary(
        ok=False, release_gate_passed=False, quarantine_state="QUARANTINED",
        fusion_verdict_class="FUSION_REGRESSIVE", safe_to_publish=False,
        safe_to_promote=False, governance_index_sha256="abc", error_code="missing_governance_index",
    )
    assert g.error_code == "missing_governance_index"


def test_guard_summary_ignores_path_like_extra() -> None:
    # Even if a path sneaks in, extra="ignore" drops it.
    g = GuardSummary.model_validate({"ok": True, "governance_index_path": "/Users/x"})
    assert "governance_index_path" not in g.model_dump()


# ── RecordListResponse ───────────────────────────────────────────────────────


def test_record_list_response_wraps_summaries() -> None:
    item = FinancialImpactSummary.from_record_dict(_full_record_dict())
    resp = RecordListResponse(items=[item])
    assert len(resp.items) == 1
    assert resp.items[0].capture_id == "cap-1"


def test_record_list_response_coerces_dicts_to_summaries() -> None:
    resp = RecordListResponse.model_validate(
        {"items": [{"capture_id": "c", "doc_id": "d", "is_finance_relevant": True,
                    "finance_relevance_score": 0.1, "created_at": "2026-01-01T00:00:00Z"}]}
    )
    assert isinstance(resp.items[0], FinancialImpactSummary)


def test_record_list_response_empty() -> None:
    with pytest.raises(ValidationError):
        RecordListResponse()  # type: ignore[call-arg]


# ── DemoRunResponse ──────────────────────────────────────────────────────────


def test_demo_run_response_basename_only_and_nested_detail() -> None:
    detail = FinancialImpactDetail(**_full_record_dict())  # type: ignore[arg-type]
    resp = DemoRunResponse(
        capture_id="cap-1", jsonl_basename="captures.jsonl",
        processed=1, skipped=0, record=detail,
    )
    # Contract: basename only, never an absolute path.
    assert "/" not in resp.jsonl_basename
    assert resp.record.capture_id == "cap-1"


# ── AppInfoResponse ──────────────────────────────────────────────────────────


def test_app_info_defaults() -> None:
    info = AppInfoResponse(
        version="1.2.3", mode="production_safe", use_ml_stubs=True,
        diagnostic_allowed=False, static_bundle_present=True,
        generated_at="2026-05-16T10:00:00Z",
    )
    assert info.name == "catchem"          # default
    assert info.commit_sha is None
    assert info.branch is None
    assert info.model_versions == {}


# ── SidecarStatusResponse ────────────────────────────────────────────────────


def test_sidecar_status_valid() -> None:
    st = SidecarStatusResponse(
        healthy=True, api_host="127.0.0.1", api_port=58744, pid=1234,
        uptime_seconds=12.5, records={"total": 3}, dlq=0,
        diagnostic_enabled=False, generated_at="2026-05-16T10:00:00Z",
    )
    assert st.api_port == 58744
    assert st.uptime_seconds == 12.5


def test_sidecar_status_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        SidecarStatusResponse(healthy=True)  # type: ignore[call-arg]


# ── LogTailResponse ──────────────────────────────────────────────────────────


def test_log_tail_response() -> None:
    lt = LogTailResponse(lines=["a", "b"], truncated=True)
    assert lt.lines == ["a", "b"]
    assert lt.truncated is True


# ── MarketQuote + batch ──────────────────────────────────────────────────────


def test_market_quote_minimal_required_fields() -> None:
    q = MarketQuote(
        symbol="AAPL", provider="fixture", retrieved_at="2026-05-16T10:00:00Z",
        market_state="closed", freshness_status="stale",
    )
    assert q.last is None
    assert q.as_of is None
    assert q.error_code is None


def test_market_quote_full() -> None:
    q = MarketQuote(
        symbol="AAPL", provider="fixture", as_of="2026-05-16T09:00:00Z",
        retrieved_at="2026-05-16T10:00:00Z", currency="USD", last=302.5,
        prev_close=300.0, change_abs=2.5, change_pct=0.83, market_state="open",
        stale_after="2026-05-16T10:05:00Z", freshness_status="fresh",
    )
    assert q.last == 302.5
    assert q.change_pct == 0.83


def test_market_quote_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        MarketQuote(symbol="AAPL")  # type: ignore[call-arg]


def test_market_quote_batch_response_round_trip() -> None:
    q = MarketQuote(
        symbol="AAPL", provider="fixture", retrieved_at="2026-05-16T10:00:00Z",
        market_state="closed", freshness_status="stale",
    )
    batch = MarketQuoteBatchResponse(
        items=[q], provider="fixture", generated_at="2026-05-16T10:00:00Z"
    )
    restored = MarketQuoteBatchResponse.model_validate_json(batch.model_dump_json())
    assert restored == batch
    assert restored.items[0].symbol == "AAPL"
