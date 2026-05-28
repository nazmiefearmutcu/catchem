"""Second-opinion reviewers.

Catchem's primary review path is the in-process `CatchemService` pipeline
(stub or HF models). This package adds *additional* reviewers — currently
DeepSeek's hosted LLM — that produce a parallel `ReviewPayload` for the
same capture, stored in the `reviews` table and surfaced on the /reviews
compare page.

Design priorities:
  * the primary path stays unchanged — every record still gets the
    in-process pipeline result and is written to the `records` table.
  * second opinions are *additive*: failure does not block ingestion, the
    cost guard is opt-in via Settings, and disabling DeepSeek reverts the
    sidecar to fully offline.
  * reviewer outputs share a normalized `ReviewPayload` shape so the
    /reviews compare page can score agreement without per-reviewer
    branches.
"""

from .base import (
    REVIEWER_STUB,
    REVIEWER_DEEPSEEK,
    ReviewerError,
    ReviewPayload,
    Reviewer,
    record_to_review_payload,
)
from .deepseek import DeepSeekReviewer
from .registry import ReviewerRegistry, build_default_registry
from .stub import StubReviewer

__all__ = [
    "REVIEWER_STUB",
    "REVIEWER_DEEPSEEK",
    "DeepSeekReviewer",
    "Reviewer",
    "ReviewerError",
    "ReviewerRegistry",
    "ReviewPayload",
    "StubReviewer",
    "build_default_registry",
    "record_to_review_payload",
]
