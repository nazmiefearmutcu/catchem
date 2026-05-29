"""Canonical catchem output schemas.

`FinancialImpactRecord` is the single durable artifact this stack produces. Every
downstream consumer (API, dashboard, exports) reads exactly this shape.

We mirror Awareness's design choice of `extra="forbid"` so additions are explicit.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# UP042 deferral: ruff wants us to inherit from `enum.StrEnum`, but
# that's a runtime-behaviour change — `str(StrEnum.A)` returns the value
# ("production_safe"), `str((str, Enum).A)` returns the qualified name
# ("ProcessingMode.PRODUCTION_SAFE"). Several JSON-export and log paths
# in the codebase format enums via `.value` already, but a downstream
# tool reading our serialized state might depend on the qualified form.
# Keep the explicit (str, Enum) idiom until that audit is done.
class ProcessingMode(str, Enum):  # noqa: UP042
    PRODUCTION_SAFE = "production_safe"
    REPLAY_EXISTING = "replay_existing"
    LIVE_TAIL = "live_tail"
    RESEARCH_DIAGNOSTIC = "research_diagnostic"


class SentimentLabel(str, Enum):  # noqa: UP042
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
                return v.replace(tzinfo=UTC)
            return v
        if isinstance(v, str):
            # Parse-and-UTC-stamp here rather than deferring to pydantic: a
            # naive ISO string (the common Awareness/RSS shape, e.g.
            # '2026-05-27T14:30:00') would otherwise parse to a NAIVE datetime
            # and this before-mode validator would never run again to stamp it,
            # silently defeating the UTC-coercion contract.
            try:
                parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return v  # not ISO; let pydantic surface the error
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed
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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("text_excerpt")
    @classmethod
    def _excerpt_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text_excerpt must be non-empty")
        return v

    @field_validator("sentiment_score")
    @classmethod
    def _finite_sentiment(cls, v: float | None) -> float | None:
        # NaN/Inf survive `finance_relevance_score`'s ge/le bounds check
        # (every comparison with NaN is False) but crash the Starlette
        # JSONResponse renderer (allow_nan=False) → HTTP 500. Map non-finite
        # to None at the contract boundary so it never reaches model_dump.
        if v is not None and not math.isfinite(v):
            return None
        return v

    @field_validator("component_scores")
    @classmethod
    def _finite_components(cls, v: dict[str, float]) -> dict[str, float]:
        # Same NaN/Inf JSON-serialization hole as sentiment_score; drop any
        # non-finite component value rather than emit an uncompliant float.
        return {
            k: f
            for k, f in v.items()
            if isinstance(f, (int, float)) and math.isfinite(f)
        }

    @field_validator("published_ts", "created_at")
    @classmethod
    def _record_utc(cls, v: datetime | None) -> datetime | None:
        # Defense-in-depth: a naive published_ts threaded in from the service
        # layer would serialize without an offset and render shifted by the
        # viewer's UTC offset in the UI; stamp naive datetimes as UTC.
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class ReplayOffset(BaseModel):
    """Where the replay/tail reader is in the JSONL stream. Persisted to SQLite."""

    model_config = ConfigDict(extra="forbid")
    source_path: str
    line_offset: int = 0
    last_capture_id: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "AwarenessCaptureView",
    "FinancialImpactRecord",
    "ProcessingMode",
    "ReplayOffset",
    "SentimentLabel",
]
