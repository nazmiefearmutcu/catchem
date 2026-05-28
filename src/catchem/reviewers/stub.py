"""Stub reviewer — wraps the in-process `CatchemService` pipeline.

This is the *primary* reviewer (writes to the `records` table) but ALSO
projects its output into the normalized `ReviewPayload` so the compare
page has a stub row to diff against. The compare math is symmetric: both
reviewers' outputs live in the `reviews` table under the same shape.
"""

from __future__ import annotations

from ..schemas import AwarenessCaptureView
from ..service import CatchemService
from .base import REVIEWER_STUB, ReviewPayload, record_to_review_payload


class StubReviewer:
    """Thin adapter — does not re-run the pipeline.

    The supervisor already called `service.process()` to produce the
    primary record; this adapter simply projects that record into a
    `ReviewPayload` so the compare table has the stub row.
    """

    reviewer_id = REVIEWER_STUB

    def __init__(self, service: CatchemService) -> None:
        self._service = service

    @property
    def reviewer_version(self) -> str:
        # Composite version string — when ANY component bumps (zero-shot,
        # sentiment, etc.) the compare page can detect drift via the
        # `reviewer_version` column without us having to rebump a global
        # "stub" version manually.
        parts = sorted(f"{k}={v}" for k, v in self._service.model_versions.items())
        return "|".join(parts) or "stub-empty"

    def review(self, cap: AwarenessCaptureView) -> ReviewPayload:
        rec = self._service.process(cap)
        return record_to_review_payload(
            rec,
            reviewer_id=self.reviewer_id,
            reviewer_version=self.reviewer_version,
        )
