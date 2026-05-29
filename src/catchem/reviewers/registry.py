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
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..logging import get_logger
from ..schemas import AwarenessCaptureView
from ..service import CatchemService
from ..settings import Settings
from ..storage import Storage
from ..taxonomy import Taxonomy
from .base import REVIEWER_DEEPSEEK, Reviewer, ReviewerError, ReviewPayload
from .deepseek import DeepSeekReviewer
from .stub import StubReviewer

logger = get_logger("catchem.reviewers.registry")

# Synthetic reviewer_id for the durable spend ledger written by the narrative
# / live-read / stream paths. These paths spend real DeepSeek money via
# `add_spend()` but never persist a `reviews` row, so without a durable record
# their spend would be lost the moment `invalidate_budget_cache()` fires (every
# PATCH /api/reviews/settings) — letting actual spend silently exceed usd_cap.
# Using a *distinct* id keeps these rows out of the stub↔deepseek compare joins
# while still being summable via `Storage.sum_review_cost`.
REVIEWER_DEEPSEEK_NARRATIVE = "deepseek_narrative"


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
    def _durable_spent_usd(self) -> float:
        """Total durable DeepSeek spend across both persisted reviews and the
        narrative spend ledger — must aggregate every USD spend or the cap is
        bypassable after a cache invalidation."""
        return float(
            self._storage.sum_review_cost(REVIEWER_DEEPSEEK)
        ) + float(self._storage.sum_review_cost(REVIEWER_DEEPSEEK_NARRATIVE))

    def budget_state(self) -> BudgetState:
        cap = float(self._settings.reviewers.deepseek.usd_cap)
        with self._lock:
            if self._cached_spent_usd is None:
                self._cached_spent_usd = self._durable_spent_usd()
            return BudgetState(spent_usd=self._cached_spent_usd, cap_usd=cap)

    def add_spend(self, usd: float) -> None:
        """Account for spend after a successful DeepSeek call.

        Persists a durable ledger row (distinct ``deepseek_narrative``
        reviewer_id, unique synthetic capture_id so rows accumulate rather
        than overwrite) *before* bumping the in-memory cache. This keeps the
        spend on disk so `invalidate_budget_cache()` → `budget_state()` can
        rebuild the true cumulative spend from SQLite and the usd_cap stays
        enforced even for the narrative / live-read / stream paths that never
        write a normal review row.
        """
        amount = max(0.0, float(usd))
        with self._lock:
            if amount > 0:
                self._storage.upsert_review(
                    {
                        "capture_id": f"spend-{uuid.uuid4().hex}",
                        "reviewer_id": REVIEWER_DEEPSEEK_NARRATIVE,
                        "reviewer_version": "deepseek-narrative-ledger",
                        "payload_json": {"kind": "spend_ledger"},
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "usd_cost": amount,
                        "latency_ms": 0,
                        "created_at": datetime.now(UTC).isoformat(),
                        "error_code": None,
                    }
                )
            if self._cached_spent_usd is None:
                self._cached_spent_usd = self._durable_spent_usd()
            else:
                self._cached_spent_usd += amount

    def _bump_cache_only(self, usd: float) -> None:
        """Bump the in-memory spend cache WITHOUT writing a durable ledger row.

        For spend that is ALREADY persisted durably elsewhere — namely
        :meth:`run_and_persist_deepseek`, which writes a normal ``reviews`` row
        (``reviewer_id=deepseek``) that :meth:`_durable_spent_usd` already sums
        via ``sum_review_cost(REVIEWER_DEEPSEEK)``. Calling :meth:`add_spend`
        there would write a SECOND durable row (``deepseek_narrative``) for the
        same dollars, so a cache rebuild after ``invalidate_budget_cache()``
        would count the spend twice and silently halve the effective
        ``usd_cap``. The narrative / live-read / stream paths that write NO
        reviews row keep calling :meth:`add_spend` (which persists the ledger).
        """
        amount = max(0.0, float(usd))
        with self._lock:
            if self._cached_spent_usd is None:
                self._cached_spent_usd = self._durable_spent_usd()
            else:
                self._cached_spent_usd += amount

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
        # Persist success + bump the cache. The upsert_review above already
        # writes the durable spend record (reviewer_id=deepseek), so bump the
        # cache ONLY here — calling add_spend would write a duplicate ledger
        # row and double-count this spend on the next cache rebuild.
        self._storage.upsert_review(payload.to_storage_row())
        self._bump_cache_only(payload.usd_cost)
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
    "REVIEWER_DEEPSEEK_NARRATIVE",
    "BudgetState",
    "ReviewerRegistry",
    "build_default_registry",
]


# Silence unused-import warnings — keeps `Reviewer` available for callers.
_REVIEWER_PROTOCOL = Reviewer
