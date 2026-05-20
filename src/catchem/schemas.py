"""Canonical catchem output schemas.

`FinancialImpactRecord` is the single durable artifact this stack produces. Every
downstream consumer (API, dashboard, exports) reads exactly this shape.

We mirror Awareness's design choice of `extra="forbid"` so additions are explicit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProcessingMode(str, Enum):
    PRODUCTION_SAFE = "production_safe"
    REPLAY_EXISTING = "replay_existing"
    LIVE_TAIL = "live_tail"
    RESEARCH_DIAGNOSTIC = "research_diagnostic"


class SentimentLabel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


class AwarenessCaptureView(BaseModel):
    """Subset of Awareness DocCapture that catchem needs.

    Mirrors `awareness.schemas.doc.DocCapture` post-commit. We don't import the
    Awareness type directly here so this module is consumable without an editable
    install of Awareness (tests can construct synthetic captures freely).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    capture_id: str
    doc_id: str
    title: str | None = None
    text: str
    language: str | None = None
    url: str | None = None
    canonical_url: str | None = None
    domain: str | None = None
    source_type: str | None = None
    discovery_channel: str | None = None
    fetch_ts: datetime | None = None
    observed_ts: datetime | None = None
    published_ts: datetime | None = None
    content_hash: str | None = None
    robots_decision: str | None = None

    @field_validator("fetch_ts", "observed_ts", "published_ts", mode="before")
    @classmethod
    def _utc(cls, v: Any) -> Any:
        if v is None or isinstance(v, datetime):
            if isinstance(v, datetime) and v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
        if isinstance(v, str):
            return v  # let pydantic parse
        return v


class FinancialImpactRecord(BaseModel):
    """The canonical output of catchem for one upstream capture."""

    model_config = ConfigDict(extra="forbid")

    # Identity / linkage to Awareness
    capture_id: str
    doc_id: str
    title: str | None = None
    text_excerpt: str
    published_ts: datetime | None = None
    domain: str | None = None
    language: str | None = None
    url: str | None = None

    # Decision layer
    is_finance_relevant: bool
    finance_relevance_score: float = Field(ge=0.0, le=1.0)

    # Multi-label taxonomy
    asset_classes: list[str] = Field(default_factory=list)
    impact_reason_codes: list[str] = Field(default_factory=list)
    candidate_symbols: list[str] = Field(default_factory=list)
    candidate_entities: list[str] = Field(default_factory=list)
    impact_horizons: list[str] = Field(default_factory=list)

    # Sentiment
    sentiment_label: SentimentLabel | None = None
    sentiment_score: float | None = None

    # Evidence
    evidence_sentences: list[str] = Field(default_factory=list)
    reason_text: str | None = None

    # Component breakdown (transparent inputs to the final decision)
    component_scores: dict[str, float] = Field(default_factory=dict)

    # Guarded diagnostic (default off)
    diagnostic_multimodal_enabled: bool = False
    diagnostic_multimodal_result: dict[str, Any] | None = None

    # Provenance
    processing_mode: ProcessingMode
    model_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("text_excerpt")
    @classmethod
    def _excerpt_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text_excerpt must be non-empty")
        return v


class ReplayOffset(BaseModel):
    """Where the replay/tail reader is in the JSONL stream. Persisted to SQLite."""

    model_config = ConfigDict(extra="forbid")
    source_path: str
    line_offset: int = 0
    last_capture_id: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = [
    "AwarenessCaptureView",
    "FinancialImpactRecord",
    "ProcessingMode",
    "ReplayOffset",
    "SentimentLabel",
]
