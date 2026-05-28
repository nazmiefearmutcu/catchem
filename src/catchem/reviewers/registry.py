"""Reviewer registry + budget guard.

The registry owns:
  * which reviewers are wired (always StubReviewer; DeepSeekReviewer if
    settings.reviewers.deepseek.enabled and an API key is present)
  * the deterministic sampling decision per capture_id
  * the running USD spend against settings.reviewers.deepseek.usd_cap

Callers (Supervisor + on-demand API endpoint) hit the registry — they
don't instantiate reviewers themselves.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any

from ..logging import get_logger
from ..schemas import AwarenessCaptureView
from ..service import CatchemService
from ..settings import Settings
from ..storage import Storage
from ..taxonomy import Taxonomy
from .base import REVIEWER_DEEPSEEK, ReviewPayload, Reviewer, ReviewerError
from .deepseek import DeepSeekReviewer
from .stub import StubReviewer

logger = get_logger("catchem.reviewers.registry")


@dataclass
class BudgetState:
    """Cumulative USD spend + remaining headroom under the cap."""

    spent_usd: float
    cap_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd


class ReviewerRegistry:
    """Owns reviewer instances, sampling logic, and budget accounting."""

    def __init__(
        self,
        *,
        settings: Settings,
        service: CatchemService,
        taxonomy: Taxonomy,
        storage: Storage,
    ) -> None:
        self._settings = settings
        self._service = service
        self._taxonomy = taxonomy
        self._storage = storage
        self._lock = threading.RLock()
        # Reviewers are constructed lazily so a missing API key doesn't
        # crash supervisor boot — DeepSeekReviewer is only built when a
        # caller actually needs it.
        self._stub: StubReviewer | None = None
        self._deepseek: DeepSeekReviewer | None = None
        # Cached cumulative spend so we don't query SQLite on every
        # sampling decision (news poller can ingest ~50 rows/min).
        self._cached_spent_usd: float | None = None

    # ── primary review (always runs) ─────────────────────────────────────
    def stub(self) -> StubReviewer:
        with self._lock:
            if self._stub is None:
                self._stub = StubReviewer(self._service)
            return self._stub

    # ── DeepSeek (opt-in) ────────────────────────────────────────────────
    def deepseek(self) -> DeepSeekReviewer | None:
        """Return the DeepSeek reviewer, or `None` if disabled / no key."""
        cfg = self._settings.reviewers.deepseek
        if not cfg.enabled:
            return None
        if not cfg.api_key:
            return None
        with self._lock:
            if self._deepseek is None:
                try:
                    self._deepseek = DeepSeekReviewer(
                        api_key=cfg.api_key,
                        taxonomy=self._taxonomy,
                        model=cfg.model,
                        base_url=cfg.base_url or "https://api.deepseek.com",
                        max_output_tokens=cfg.max_output_tokens,
                    )
                except ReviewerError as exc:
                    logger.warning("deepseek_init_failed", code=exc.code, message=exc.message)
                    return None
            return self._deepseek

    # ── sampling decision ────────────────────────────────────────────────
    def should_sample_for_deepseek(self, capture_id: str) -> bool:
        """Deterministic %-based sampling. Same capture_id → same decision.

        Returns False if DeepSeek is disabled / unkeyed / budget exhausted.
        Returns False on every sample if `sampling_rate` is 0; True for
        every sample if 1.0 — operators can pin the rate to those bounds
        for deterministic testing.
        """
        cfg = self._settings.reviewers.deepseek
        if not cfg.enabled or not cfg.api_key:
            return False
        rate = float(cfg.sampling_rate)
        if rate <= 0:
            return False
        if rate >= 1:
            sampled = True
        else:
            # SHA-256 over the capture_id, mod 10_000 → stable 0-9999.
            # Compares against `rate * 10_000` so floats like 0.05 give
            # exactly 500 buckets, 0.1 gives 1000 buckets, etc.
            digest = hashlib.sha256(capture_id.encode("utf-8")).hexdigest()
            bucket = int(digest[:8], 16) % 10_000
            sampled = bucket < int(rate * 10_000)
        if not sampled:
            return False
        return not self.budget_state().exhausted

    # ── budget accounting ────────────────────────────────────────────────
    def budget_state(self) -> BudgetState:
        cap = float(self._settings.reviewers.deepseek.usd_cap)
        with self._lock:
            if self._cached_spent_usd is None:
                self._cached_spent_usd = float(
                    self._storage.sum_review_cost(REVIEWER_DEEPSEEK)
                )
            return BudgetState(spent_usd=self._cached_spent_usd, cap_usd=cap)

    def add_spend(self, usd: float) -> None:
        """Increment the cached spend after a successful DeepSeek call."""
        with self._lock:
            if self._cached_spent_usd is None:
                self._cached_spent_usd = float(
                    self._storage.sum_review_cost(REVIEWER_DEEPSEEK)
                )
            self._cached_spent_usd += max(0.0, float(usd))

    def invalidate_budget_cache(self) -> None:
        """Drop the cached spend so the next `budget_state` reads SQLite."""
        with self._lock:
            self._cached_spent_usd = None

    # ── run + persist ────────────────────────────────────────────────────
    def run_and_persist_deepseek(
        self, cap: AwarenessCaptureView
    ) -> ReviewPayload | None:
        """Synchronous helper used by the on-demand endpoint + sampler.

        Returns the persisted payload on success, `None` if DeepSeek is
        not configured. Errors are caught and persisted as a row with
        `error_code` populated — the caller can render the failure
        instead of losing the slot.
        """
        client = self.deepseek()
        if client is None:
            return None
        if self.budget_state().exhausted:
            return self._persist_error(cap, "budget_exceeded", "USD cap reached")
        try:
            payload = client.review(cap)
        except ReviewerError as exc:
            logger.warning(
                "deepseek_review_failed",
                code=exc.code,
                message=exc.message,
                capture_id=cap.capture_id,
            )
            return self._persist_error(cap, exc.code, exc.message)
        # Persist success + bump the cache.
        self._storage.upsert_review(payload.to_storage_row())
        self.add_spend(payload.usd_cost)
        return payload

    def _persist_error(
        self, cap: AwarenessCaptureView, code: str, message: str
    ) -> ReviewPayload:
        """Write an error-shaped review row so the compare page can show it."""
        empty_payload = ReviewPayload(
            capture_id=cap.capture_id,
            reviewer_id=REVIEWER_DEEPSEEK,
            reviewer_version=self.deepseek().reviewer_version if self.deepseek() else "deepseek-unconfigured",
            is_finance_relevant=False,
            finance_relevance_score=0.0,
            asset_classes=(),
            impact_reason_codes=(),
            candidate_symbols=(),
            sentiment_label=None,
            sentiment_score=None,
            evidence_sentences=(),
            reason_text=message,
            error_code=code,
            raw_response={"error": message},
        )
        self._storage.upsert_review(empty_payload.to_storage_row())
        return empty_payload

    # ── helpers for tests / introspection ────────────────────────────────
    def status(self) -> dict[str, Any]:
        cfg = self._settings.reviewers.deepseek
        client = self.deepseek()
        budget = self.budget_state()
        return {
            "deepseek_enabled": bool(cfg.enabled),
            "deepseek_keyed": bool(cfg.api_key),
            "deepseek_ready": client is not None,
            "model": cfg.model,
            "sampling_rate": cfg.sampling_rate,
            "usd_cap": budget.cap_usd,
            "usd_spent": round(budget.spent_usd, 6),
            "usd_remaining": round(budget.remaining_usd, 6),
            "exhausted": budget.exhausted,
        }


def build_default_registry(
    settings: Settings,
    service: CatchemService,
    taxonomy: Taxonomy,
    storage: Storage,
) -> ReviewerRegistry:
    """One-line factory used by Supervisor."""
    return ReviewerRegistry(
        settings=settings,
        service=service,
        taxonomy=taxonomy,
        storage=storage,
    )


__all__ = [
    "BudgetState",
    "ReviewerRegistry",
    "build_default_registry",
]


# Silence unused-import warnings — keeps `Reviewer` available for callers.
_REVIEWER_PROTOCOL = Reviewer
