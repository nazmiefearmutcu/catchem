"""Shared reviewer types.

`ReviewPayload` is the normalized shape every reviewer produces, derived
from `FinancialImpactRecord` so the compare page can score agreement
without per-reviewer branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..schemas import AwarenessCaptureView, FinancialImpactRecord

# Canonical reviewer IDs. Anything written to `reviews.reviewer_id` MUST
# come from this set so the compare page joins line up.
REVIEWER_STUB = "stub"
REVIEWER_DEEPSEEK = "deepseek"


class ReviewerError(Exception):
    """Reviewer-side failure surfaced to storage as a typed row.

    `code` is a short machine-readable token (`auth`, `rate_limit`,
    `timeout`, `bad_json`, `budget_exceeded`, `network`) so the compare
    page can render a friendly chip without parsing arbitrary text.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ReviewPayload:
    """Normalized review output.

    The structural fields mirror `FinancialImpactRecord` so the compare
    page can join on `(capture_id, reviewer_id)` and score agreement
    field-by-field. Token/cost meta lives alongside so we can audit
    spend per reviewer without a separate ledger.
    """

    capture_id: str
    reviewer_id: str
    reviewer_version: str
    is_finance_relevant: bool
    finance_relevance_score: float
    asset_classes: tuple[str, ...]
    impact_reason_codes: tuple[str, ...]
    candidate_symbols: tuple[str, ...]
    sentiment_label: str | None
    sentiment_score: float | None
    evidence_sentences: tuple[str, ...]
    reason_text: str | None = None
    # Meta — populated by the API-backed reviewer; stub-side stays zero.
    input_tokens: int = 0
    output_tokens: int = 0
    usd_cost: float = 0.0
    latency_ms: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    error_code: str | None = None
    raw_response: dict[str, Any] | None = None

    def to_storage_row(self) -> dict[str, Any]:
        """Shape for `Storage.upsert_review()` — JSON-serialized payload."""
        return {
            "capture_id": self.capture_id,
            "reviewer_id": self.reviewer_id,
            "reviewer_version": self.reviewer_version,
            "payload_json": self._payload_for_json(),
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "usd_cost": float(self.usd_cost),
            "latency_ms": int(self.latency_ms),
            "created_at": self.created_at,
            "error_code": self.error_code,
        }

    def _payload_for_json(self) -> dict[str, Any]:
        """Stable shape that survives JSON round-trip."""
        return {
            "is_finance_relevant": bool(self.is_finance_relevant),
            "finance_relevance_score": float(self.finance_relevance_score),
            "asset_classes": list(self.asset_classes),
            "impact_reason_codes": list(self.impact_reason_codes),
            "candidate_symbols": list(self.candidate_symbols),
            "sentiment_label": self.sentiment_label,
            "sentiment_score": (
                float(self.sentiment_score) if self.sentiment_score is not None else None
            ),
            "evidence_sentences": list(self.evidence_sentences),
            "reason_text": self.reason_text,
            "raw": self.raw_response,
        }


def record_to_review_payload(
    rec: FinancialImpactRecord,
    reviewer_id: str,
    reviewer_version: str,
) -> ReviewPayload:
    """Project a `FinancialImpactRecord` into the normalized payload.

    Used by the in-process StubReviewer so both reviewers write into
    `reviews` with the same shape — the compare page never has to know
    which reviewer produced which row.
    """
    return ReviewPayload(
        capture_id=rec.capture_id,
        reviewer_id=reviewer_id,
        reviewer_version=reviewer_version,
        is_finance_relevant=bool(rec.is_finance_relevant),
        finance_relevance_score=float(rec.finance_relevance_score),
        asset_classes=tuple(rec.asset_classes),
        impact_reason_codes=tuple(rec.impact_reason_codes),
        candidate_symbols=tuple(rec.candidate_symbols),
        sentiment_label=rec.sentiment_label,
        sentiment_score=(
            float(rec.sentiment_score) if rec.sentiment_score is not None else None
        ),
        evidence_sentences=tuple(rec.evidence_sentences),
        reason_text=rec.reason_text,
        # Meta defaults to zero for the in-process reviewer — it doesn't
        # have a token budget to spend.
    )


class Reviewer(Protocol):
    """Reviewer interface — produces a `ReviewPayload` for a capture.

    Implementations:
      * `StubReviewer` — wraps the in-process pipeline
      * `DeepSeekReviewer` — hosted LLM via HTTPS

    `review()` may raise `ReviewerError` on transport / budget / parse
    failure; callers should write a row with `error_code` set so the
    compare page can render the failure without losing the slot.
    """

    reviewer_id: str
    reviewer_version: str

    def review(self, cap: AwarenessCaptureView) -> ReviewPayload: ...
