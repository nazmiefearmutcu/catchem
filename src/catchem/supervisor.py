"""Coordinator. One place that owns Settings → Storage → Service → Replay/Tail.

Both CLI and API construct a Supervisor and call ``run_*`` / ``process_one``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

from .awareness_reader import discover_awareness_jsonl_root
from .awareness_replay import ReplayRunner
from .embeddings import VectorIndex
from .logging import configure_logging, get_logger
from .reviewers import (
    REVIEWER_DEEPSEEK,
    ReviewerRegistry,
    build_default_registry,
    record_to_review_payload,
)
from .schemas import AwarenessCaptureView, FinancialImpactRecord
from .service import CatchemService, build_service
from .settings import Settings
from .storage import load_storage_from_settings
from .taxonomy import default_taxonomy_path, load_taxonomy
from .webhook import send_webhook

logger = get_logger("catchem.supervisor")


class Supervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        configure_logging(
            level=settings.logging.level,
            log_file=settings.paths.catchem_output_dir / Path(settings.logging.file).relative_to("data") if settings.logging.file.startswith("data/") else None,
            json_mode=settings.logging.json_logs,
        )
        self.storage = load_storage_from_settings(settings)
        vector_dir = settings.paths.catchem_output_dir / Path(settings.storage.vector_index_dir).relative_to("data") if settings.storage.vector_index_dir.startswith("data/") else settings.paths.catchem_output_dir / "vector_index"
        self.vector_index = VectorIndex(vector_dir)
        self.service: CatchemService = build_service(settings, vector_index=self.vector_index)
        # Second-opinion reviewer plumbing. The registry is cheap to
        # construct (lazy DeepSeek init) so we always wire it; it's the
        # `should_sample_for_deepseek` gate that decides whether any
        # external call actually fires.
        self.reviewers: ReviewerRegistry = build_default_registry(
            settings=settings,
            service=self.service,
            taxonomy=load_taxonomy(default_taxonomy_path()),
            storage=self.storage,
        )
        # Bounded thread pool — DeepSeek calls are 1-5s each, and the
        # news poller can ingest ~50 rows/min during a busy news window.
        # Two workers + the deterministic sampling rate keep us under the
        # DeepSeek free-tier rate-limit (60 RPM) by a wide margin.
        self._deepseek_pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="catchem-deepseek",
        )
        # Separate fire-and-forget pool for webhook POSTs so a stuck
        # webhook can never starve DeepSeek's call queue (or vice-versa).
        # Two workers is plenty: webhook traffic is rare (only
        # high-score arrivals) and short (single POST + parse).
        self._webhook_pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="catchem-webhook",
        )
        # Aggregate counters surfaced via /api/webhook/status. In-memory
        # only — restarting the sidecar resets these. That's fine; the
        # operator just wants to know "did anything fire this session?".
        self.webhook_stats: dict[str, int] = {
            "attempted": 0,
            "sent": 0,
            "filtered": 0,
            "failed": 0,
        }
        # webhook_stats is mutated from SEVERAL threads: `attempted` on the
        # ingest worker(s) — process_capture runs in the poller's to_thread
        # pool (up to 4) plus the WS-push reader — and sent/filtered/failed on
        # the _webhook_pool workers. `dict[key] += 1` is a non-atomic
        # read-modify-write, so concurrent increments silently drop counts.
        # This lock serialises every mutation (and snapshot).
        self._webhook_stats_lock = threading.Lock()
        self.webhook_last_status: str | None = None
        self.webhook_last_error: str | None = None
        logger.info(
            "supervisor_initialized",
            mode=settings.mode.value,
            diagnostic_enabled=self.service.diagnostic_enabled,
            stubs=settings.models.use_ml_stubs,
            deepseek_enabled=settings.reviewers.deepseek.enabled,
            deepseek_keyed=bool(settings.reviewers.deepseek.api_key),
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def close(self) -> None:
        with suppress(Exception):
            self._deepseek_pool.shutdown(wait=False, cancel_futures=True)
        with suppress(Exception):
            self._webhook_pool.shutdown(wait=False, cancel_futures=True)
        with suppress(Exception):
            self.storage.close()

    # ── single-capture path (used by API /process-one) ───────────────────
    def process_capture(self, cap: AwarenessCaptureView) -> FinancialImpactRecord:
        rec = self.service.process(cap)
        self.storage.insert_record(rec)
        self._maybe_schedule_second_opinion(cap, rec)
        self._maybe_dispatch_webhook(rec)
        return rec

    # ── webhook fire-and-forget ──────────────────────────────────────────
    def _maybe_dispatch_webhook(self, rec: FinancialImpactRecord) -> None:
        """Dispatch a Slack/Discord/Teams webhook for high-score arrivals.

        Filtering (score floor, asset_class, reason_code) lives in
        `webhook.should_send`. The thread pool ensures slow HTTP never
        blocks ingestion. The supervisor counter aggregates outcomes so
        the UI can show "12 sent · 3 filtered · 1 failed" without
        scraping logs.
        """
        cfg = self.settings.webhook
        if not cfg.enabled or not cfg.url:
            return
        try:
            payload = rec.model_dump(mode="json")
        except Exception as exc:
            logger.warning("webhook_serialize_failed", error=str(exc))
            return
        with self._webhook_stats_lock:
            self.webhook_stats["attempted"] += 1
        try:
            self._webhook_pool.submit(self._webhook_runner, payload)
        except RuntimeError:
            # Executor shut down during teardown — no need to log loudly.
            logger.debug("webhook_executor_unavailable", capture_id=rec.capture_id)

    def _webhook_runner(self, record: dict[str, Any]) -> None:
        """Thread-pool entry point. Updates counters; never raises."""
        ok, status = send_webhook(record, self.settings.webhook)  # network — outside the lock
        self.webhook_last_status = status
        if ok:
            with self._webhook_stats_lock:
                self.webhook_stats["sent"] += 1
            self.webhook_last_error = None
        elif status == "filtered":
            # filtered is the by-design "skipped" path — don't count it
            # as a failure. The `attempted` counter still ticked because
            # we *tried* to dispatch.
            with self._webhook_stats_lock:
                self.webhook_stats["filtered"] += 1
        else:
            with self._webhook_stats_lock:
                self.webhook_stats["failed"] += 1
            self.webhook_last_error = status
            logger.info(
                "webhook_post_failed",
                status=status,
                capture_id=record.get("capture_id"),
            )

    def webhook_stats_snapshot(self) -> dict[str, int]:
        """A consistent copy of the webhook counters, taken under the lock so
        callers (e.g. /api/webhook/status) never read a half-applied set."""
        with self._webhook_stats_lock:
            return dict(self.webhook_stats)

    # ── second-opinion hook (deterministic sampling + budget guard) ──────
    def _maybe_schedule_second_opinion(
        self, cap: AwarenessCaptureView, rec: FinancialImpactRecord
    ) -> None:
        """Persist the stub review row, then async-fire DeepSeek if sampled.

        Errors (network/parse/budget) write a row with `error_code` set
        so the compare page can still render the slot — they never
        propagate back to ingestion.
        """
        if not self.reviewers.should_sample_for_deepseek(cap.capture_id):
            return
        # Stub row paired with the primary record so the compare page has
        # both sides. We project — no re-run of `service.process()`.
        try:
            stub_payload = record_to_review_payload(
                rec,
                reviewer_id=self.reviewers.stub().reviewer_id,
                reviewer_version=self.reviewers.stub().reviewer_version,
            )
            self.storage.upsert_review(stub_payload.to_storage_row())
        except Exception as exc:
            logger.warning("stub_review_persist_failed", error=str(exc))
            return
        # Fire-and-forget DeepSeek call. The registry handles persistence
        # and budget accounting; we don't block ingestion on the result.
        try:
            self._deepseek_pool.submit(
                self.reviewers.run_and_persist_deepseek, cap
            )
        except RuntimeError:
            # Executor shut down — happens during process teardown. Log
            # and move on; the row is just missing the DeepSeek side.
            logger.debug("deepseek_executor_unavailable", capture_id=cap.capture_id)

    # ── replay ───────────────────────────────────────────────────────────
    def run_replay(
        self, max_records: int | None = None, *, file: Path | None = None
    ) -> dict[str, Any]:
        """Replay finalized Awareness JSONL into storage.

        ``file`` honors the CLI's documented single-file contract
        (``catchem replay --path FILE``): when given, ONLY that one file is
        ingested rather than every ``*.jsonl`` under its parent. It is scoped
        by pointing the runner's root at the file's parent and using the exact
        filename as the glob pattern, so siblings/descendants are not swept in.
        ``file=None`` keeps the directory-discovery behavior (recursive
        ``**/*.jsonl`` under the configured awareness data dir).
        """
        if file is not None:
            root = file.parent
            # Escape glob metacharacters in the literal filename — a real file
            # named e.g. `2026-05-30[backup].jsonl` or one containing `*`/`?`
            # would otherwise be interpreted as a glob pattern and match the
            # wrong files (or nothing), silently replaying something other than
            # the single file the operator pointed at.
            import glob as _glob

            pattern = _glob.escape(file.name)
        else:
            root = discover_awareness_jsonl_root(self.settings.paths.awareness_data_dir)
            # Honor the configured replay glob (relative to the discovered
            # root) so `replay.awareness_jsonl_glob` actually scopes the scan
            # rather than being dead config.
            pattern = self.settings.replay.replay_pattern()
        runner = ReplayRunner(root=root, storage=self.storage, pattern=pattern, offset_persist_seconds=self.settings.replay.offset_persist_seconds)
        records_before = self.storage.count_records()
        dlq_before = self.storage.dlq_count()
        inserted = 0
        replaced = 0

        def handle(cap: AwarenessCaptureView) -> None:
            nonlocal inserted, replaced
            rec = self.service.process(cap)
            was_inserted = self.storage.insert_record(rec)
            self._maybe_schedule_second_opinion(cap, rec)
            # Webhook only on freshly-inserted records — replaying the
            # same JSONL shouldn't spam the channel with duplicate alerts.
            if was_inserted:
                self._maybe_dispatch_webhook(rec)
                inserted += 1
            else:
                replaced += 1

        counts = runner.run_once(handle, max_records=max_records)
        self.storage.flush()
        records_after = self.storage.count_records()
        dlq_after = self.storage.dlq_count()
        dlq_delta = max(0, dlq_after - dlq_before)
        return {
            **counts,
            "failed": counts.get("failed", dlq_delta),
            "dlq": dlq_after,
            "dlq_delta": dlq_delta,
            "records_before": records_before,
            "records_after": records_after,
            "inserted": inserted,
            "replaced": replaced,
            "net_new_records": max(0, records_after.get("total", 0) - records_before.get("total", 0)),
        }

    # ── live tail ────────────────────────────────────────────────────────
    def run_tail(self, stop: Any = None) -> None:
        root = discover_awareness_jsonl_root(self.settings.paths.awareness_data_dir)
        runner = ReplayRunner(root=root, storage=self.storage, offset_persist_seconds=self.settings.replay.offset_persist_seconds)

        def handle(cap: AwarenessCaptureView) -> None:
            try:
                self.process_capture(cap)
            except Exception as exc:
                self.storage.record_failure(cap.capture_id, str(exc), (cap.text or "")[:2000])

        runner.tail(
            handle,
            poll_seconds=self.settings.live.poll_seconds,
            max_per_tick=self.settings.live.tail_max_per_tick,
            stop=stop,
        )

    # ── status / metrics ─────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        counts = self.storage.count_records()
        return {
            "mode": self.settings.mode.value,
            "diagnostic_enabled": self.service.diagnostic_enabled,
            "use_ml_stubs": self.settings.models.use_ml_stubs,
            "records": counts,
            "dlq": self.storage.dlq_count(),
            "model_versions": dict(self.service.model_versions),
            "reviewers": self.reviewers.status(),
        }

    # Re-export for tests / API: which reviewer ID the second-opinion path uses.
    DEEPSEEK_REVIEWER_ID = REVIEWER_DEEPSEEK
