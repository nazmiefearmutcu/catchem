"""Focused unit tests for the orchestrator (`catchem.supervisor.Supervisor`).

These complement the integration-marked `test_service_replay_mode.py` (which
already exercises the happy/idempotent/failure `run_replay` paths) by pinning
the *units* the integration tests skip: `status()` shape, `process_capture`,
the webhook fire-and-forget counters, the second-opinion stub-row persistence
+ budget-cache invalidation hook, `run_tail` stop semantics, and idempotent
`close()`.

Everything here is offline and deterministic:
  * `use_ml_stubs=true` (the autouse conftest fixture sets it) — no real ML.
  * The DeepSeek reviewer is never *called* — we either keep it disabled or
    drive the stub-row path with the executor torn down, so no HTTP fires.
  * The webhook paths used here short-circuit in `should_send` *before* any
    `httpx.post`, so no network is touched either.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catchem.settings import load_settings, reload_settings
from catchem.supervisor import Supervisor


def _make_supervisor(
    tmp_path: Path,
    write_jsonl,
    synth_capture,
    monkeypatch: pytest.MonkeyPatch,
    rows: list | None = None,
) -> Supervisor:
    """Build a Supervisor pointed at a fresh tmp data dir + synthetic JSONL.

    Mirrors the construction the integration suite uses so the wiring under
    test (Settings → Storage → Service → reviewers) is the real thing.
    """
    if rows is None:
        rows = [json.loads(synth_capture().model_dump_json())]
    write_jsonl(rows)
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    # The repo's local `.env` enables a real DeepSeek key. Process env beats
    # `.env` in this project's settings precedence, so we hard-disable the
    # reviewer + scrub the key here to guarantee a hermetic, offline default.
    # Tests that need sampling=True re-enable it AFTER tearing down the pool.
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__ENABLED", "false")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__API_KEY", "")
    monkeypatch.setenv("CATCHEM_WEBHOOK__ENABLED", "false")
    monkeypatch.setenv("CATCHEM_WEBHOOK__URL", "")
    reload_settings()
    return Supervisor(load_settings())


# ── status() ──────────────────────────────────────────────────────────────
def test_status_shape_on_empty_storage(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        st = sup.status()
        assert set(st) >= {
            "mode",
            "diagnostic_enabled",
            "use_ml_stubs",
            "records",
            "dlq",
            "model_versions",
            "reviewers",
        }
        # Production-safe default mode; stubs forced on by the conftest fixture.
        assert st["mode"] == "production_safe"
        assert st["use_ml_stubs"] is True
        assert st["diagnostic_enabled"] is False
        # Nothing ingested yet.
        assert st["records"] == {"total": 0, "finance_relevant": 0}
        assert st["dlq"] == 0
        # model_versions is a plain dict snapshot of the service.
        assert isinstance(st["model_versions"], dict) and st["model_versions"]
        # reviewers status is the registry's own envelope.
        rv = st["reviewers"]
        assert rv["deepseek_enabled"] is False
        assert rv["exhausted"] is False
        assert rv["usd_spent"] == 0.0
    finally:
        sup.close()


def test_status_reflects_records_after_process(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        rec = sup.process_capture(synth_capture(capture_id="c-stat", doc_id="d-stat"))
        assert rec.capture_id == "c-stat"
        st = sup.status()
        assert st["records"]["total"] == 1
        # The Fed-rates default text is finance-relevant under the stub pipeline.
        assert st["records"]["finance_relevant"] == 1
    finally:
        sup.close()


# ── process_capture() single-capture path ──────────────────────────────────
def test_process_capture_inserts_and_is_retrievable(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        cap = synth_capture(capture_id="c-proc", doc_id="d-proc")
        rec = sup.process_capture(cap)
        assert rec.capture_id == "c-proc"
        stored = sup.storage.get_record("c-proc")
        assert stored is not None
        assert stored["is_finance_relevant"] is True
        assert sup.storage.count_records()["total"] == 1
    finally:
        sup.close()


# ── webhook fire-and-forget counters ────────────────────────────────────────
def test_webhook_disabled_is_a_noop(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        # Webhook config defaults to disabled — dispatch must not even count.
        sup.process_capture(synth_capture(capture_id="c-nohook", doc_id="d-nohook"))
        assert sup.webhook_stats["attempted"] == 0
        assert sup.webhook_stats["sent"] == 0
    finally:
        sup.close()


def test_webhook_enabled_filtered_increments_filtered_counter(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled + valid URL but min_score above the record's score → 'filtered'.

    `should_send` rejects on the score floor *before* any HTTP call, so this
    stays fully offline while still driving the runner's filtered branch.
    """
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        sup.settings.webhook.enabled = True
        sup.settings.webhook.url = "https://hooks.slack.com/services/T000/B000/xxxx"
        sup.settings.webhook.min_score = 1.01  # impossible → always filtered
        sup.process_capture(synth_capture(capture_id="c-filt", doc_id="d-filt"))
        sup._webhook_pool.shutdown(wait=True)  # drain the background runner
        assert sup.webhook_stats["attempted"] == 1
        assert sup.webhook_stats["filtered"] == 1
        assert sup.webhook_stats["sent"] == 0
        assert sup.webhook_stats["failed"] == 0
    finally:
        sup.close()


def test_webhook_invalid_url_increments_failed_counter(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled with a non-HTTP URL → 'invalid_url' status, counted as failed.

    `send_webhook` returns ('invalid_url') from `is_valid_webhook_url` before
    touching the network.
    """
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        sup.settings.webhook.enabled = True
        sup.settings.webhook.url = "ftp://not-a-webhook"
        sup.settings.webhook.min_score = 0.0  # clear the score floor
        sup.process_capture(synth_capture(capture_id="c-badurl", doc_id="d-badurl"))
        sup._webhook_pool.shutdown(wait=True)
        assert sup.webhook_stats["attempted"] == 1
        assert sup.webhook_stats["failed"] == 1
        assert sup.webhook_last_status == "invalid_url"
        assert sup.webhook_last_error == "invalid_url"
    finally:
        sup.close()


def test_webhook_dispatch_after_pool_shutdown_does_not_raise(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-score arrival after teardown hits the executor-unavailable branch.

    `attempted` still ticks (we *tried*), but `submit` raising RuntimeError is
    swallowed — ingestion must never break on a torn-down pool.
    """
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        sup.settings.webhook.enabled = True
        sup.settings.webhook.url = "https://hooks.slack.com/services/T000/B000/xxxx"
        sup.settings.webhook.min_score = 0.0
        sup._webhook_pool.shutdown(wait=True)  # force submit() to raise
        sup.process_capture(synth_capture(capture_id="c-down", doc_id="d-down"))
        assert sup.webhook_stats["attempted"] == 1
        assert sup.webhook_stats["sent"] == 0
    finally:
        sup.close()


# ── second-opinion hook (stub-row persist + budget cache) ───────────────────
def test_second_opinion_persists_stub_review_when_sampled(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sampling-rate 1.0 + keyed DeepSeek → the stub review row is persisted.

    We tear down the DeepSeek pool first so the fire-and-forget DeepSeek
    submit raises RuntimeError (caught, logged) — meaning NO external call can
    fire. What we assert is the synchronous half: the projected stub review row
    landed in the `reviews` table, paired with the primary record.
    """
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        # Make sampling deterministic-True without enabling a real network call.
        sup.settings.reviewers.deepseek.enabled = True
        sup.settings.reviewers.deepseek.api_key = "sk-test-not-real"
        sup.settings.reviewers.deepseek.sampling_rate = 1.0
        assert sup.reviewers.should_sample_for_deepseek("c-rev") is True
        # Kill the DeepSeek executor so the async DeepSeek call can't run.
        sup._deepseek_pool.shutdown(wait=True)

        cap = synth_capture(capture_id="c-rev", doc_id="d-rev")
        rec = sup.process_capture(cap)
        assert rec.capture_id == "c-rev"

        # The stub side of the compare pair must be persisted synchronously.
        stub_id = sup.reviewers.stub().reviewer_id
        rows = sup.storage.get_reviews_for_capture("c-rev")
        stub_rows = [r for r in rows if r["reviewer_id"] == stub_id]
        assert len(stub_rows) == 1
        assert stub_rows[0]["capture_id"] == "c-rev"
    finally:
        sup.close()


def test_second_opinion_skipped_when_not_sampled(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sampling-rate 0 → no review row is written at all (fast early return)."""
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        sup.settings.reviewers.deepseek.enabled = True
        sup.settings.reviewers.deepseek.api_key = "sk-test-not-real"
        sup.settings.reviewers.deepseek.sampling_rate = 0.0
        assert sup.reviewers.should_sample_for_deepseek("c-norev") is False

        sup.process_capture(synth_capture(capture_id="c-norev", doc_id="d-norev"))
        assert sup.storage.get_reviews_for_capture("c-norev") == []
    finally:
        sup.close()


def test_budget_cache_invalidation_hook_rereads_storage(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`invalidate_budget_cache()` drops the cached spend so the next read
    hits SQLite again — the post-ingest hook the API relies on."""
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        # Prime the cache.
        first = sup.reviewers.budget_state()
        assert first.spent_usd == 0.0
        # Simulate spend accounting, then invalidate.
        sup.reviewers.add_spend(1.25)
        assert sup.reviewers.budget_state().spent_usd == pytest.approx(1.25)
        sup.reviewers.invalidate_budget_cache()
        # Storage has no review rows, so a fresh read returns 0 again.
        assert sup.reviewers.budget_state().spent_usd == 0.0
    finally:
        sup.close()


# ── run_tail stop semantics ─────────────────────────────────────────────────
def test_run_tail_returns_immediately_when_stop_is_set(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop predicate that fires after the first tick makes `run_tail` return.

    `ReplayRunner.tail` takes `stop` as a zero-arg callable returning bool and
    checks it at the TOP of each loop iteration. A predicate that returns True
    on the first check exits before any sleep — deterministic and fast.
    """
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        # Tighten the poll so even if a tick runs it returns fast.
        sup.settings.live.poll_seconds = 0.01
        calls = {"n": 0}

        def stop() -> bool:
            # Let exactly one full tick run, then halt.
            calls["n"] += 1
            return calls["n"] > 1

        sup.run_tail(stop=stop)  # must return, not hang
        assert calls["n"] >= 1
        # The single synthetic row was ingested by the one tick that ran.
        assert sup.storage.count_records()["total"] == 1
    finally:
        sup.close()


def test_run_tail_routes_handler_failure_to_dlq(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `process_capture` raises inside the tail handler, the capture is
    recorded as a DLQ failure instead of crashing the loop."""
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    try:
        sup.settings.live.poll_seconds = 0.01

        def boom(_cap):
            raise RuntimeError("synthetic tail failure")

        monkeypatch.setattr(sup.service, "process", boom)
        calls = {"n": 0}

        def stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        sup.run_tail(stop=stop)
        # Handler failure → DLQ row, no successful record.
        assert sup.storage.dlq_count() == 1
        assert sup.storage.count_records()["total"] == 0
    finally:
        sup.close()


# ── close() idempotency ─────────────────────────────────────────────────────
def test_close_is_idempotent(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _make_supervisor(tmp_path, write_jsonl, synth_capture, monkeypatch)
    sup.close()
    # Second + third close must not raise (pools + storage already shut down).
    sup.close()
    sup.close()
