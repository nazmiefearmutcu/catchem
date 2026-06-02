from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime, timedelta
import pytest
from catchem.settings import load_settings, reload_settings
from catchem.supervisor import Supervisor
from catchem.schemas import AwarenessCaptureView, FinancialImpactRecord
from catchem.demo import build_capture

@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "true")
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    monkeypatch.setenv("CATCHEM_WEBHOOK__ENABLED", "true")
    monkeypatch.setenv("CATCHEM_WEBHOOK__URL", "https://example.com/webhook")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__ENABLED", "true")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__API_KEY", "test-key")
    # Always sample
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__SAMPLING_RATE", "1.0")
    reload_settings()
    sup = Supervisor(load_settings())
    yield sup
    sup.close()

def test_webhook_serialize_failed(supervisor) -> None:
    # Trigger line 129-133: webhook serialization raises Exception
    from catchem.schemas import ProcessingMode
    class BadRecord(FinancialImpactRecord):
        def model_dump(self, *args, **kwargs):
            raise ValueError("mock serialize error")

    base = datetime.now(UTC)
    bad_rec = BadRecord(
        capture_id="cap-bad-1",
        doc_id="doc-bad-1",
        title="Bad Title",
        text_excerpt="Bad Excerpt",
        domain="bad.com",
        url="http://bad.com",
        is_finance_relevant=True,
        finance_relevance_score=0.9,
        processing_mode=ProcessingMode.REPLAY_EXISTING,
    )
    supervisor._maybe_dispatch_webhook(bad_rec)
    assert supervisor.webhook_stats["attempted"] == 0

def test_webhook_pool_shutdown_runtime_error(supervisor) -> None:
    # Trigger line 138-140: _webhook_pool.submit raises RuntimeError
    supervisor._webhook_pool.shutdown(wait=True)
    
    rec = supervisor.service.process(build_capture(title="Title", text="Text"))
    supervisor._maybe_dispatch_webhook(rec)
    assert supervisor.webhook_stats["attempted"] == 1

def test_webhook_runner_filtered_and_failed(supervisor, monkeypatch) -> None:
    # Trigger line 150-164: webhook runner filtered and failed status paths
    import catchem.supervisor as cs
    
    # 1. Filtered status
    def mock_send_webhook_filtered(rec, cfg):
        return False, "filtered"
    monkeypatch.setattr(cs, "send_webhook", mock_send_webhook_filtered)
    
    supervisor._webhook_runner({"capture_id": "c1"})
    assert supervisor.webhook_stats["filtered"] == 1

    # 2. Failed status
    def mock_send_webhook_failed(rec, cfg):
        return False, "500 Internal Server Error"
    monkeypatch.setattr(cs, "send_webhook", mock_send_webhook_failed)
    
    supervisor._webhook_runner({"capture_id": "c2"})
    assert supervisor.webhook_stats["failed"] == 1
    assert supervisor.webhook_last_error == "500 Internal Server Error"

def test_maybe_schedule_second_opinion_upsert_review_failed(supervisor, monkeypatch) -> None:
    # Trigger line 193-195: upsert_review raises exception
    def mock_upsert_review(row):
        raise ValueError("mock upsert fail")
    monkeypatch.setattr(supervisor.storage, "upsert_review", mock_upsert_review)
    
    cap = build_capture(title="Sample title", text="Sample text")
    rec = supervisor.service.process(cap)
    
    supervisor._maybe_schedule_second_opinion(cap, rec)

def test_maybe_schedule_second_opinion_deepseek_pool_shutdown_runtime_error(supervisor) -> None:
    # Trigger line 202-205: deepseek_pool.submit raises RuntimeError
    supervisor._deepseek_pool.shutdown(wait=True)
    
    cap = build_capture(title="Sample title", text="Sample text")
    rec = supervisor.service.process(cap)
    
    supervisor._maybe_schedule_second_opinion(cap, rec)

def test_replay_file_glob_metachars(supervisor, tmp_path) -> None:
    # Trigger line 222-230: file is not None and pattern glob escape
    replay_file = tmp_path / "manifests" / "2026-05-30[backup].jsonl"
    replay_file.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    cap_data = {
        "capture_id": "glob-1",
        "doc_id": "glob-doc-1",
        "title": "Fed rate hike",
        "text": "The Fed hiked rates.",
        "domain": "reuters.com",
    }
    replay_file.write_text(_json.dumps(cap_data) + "\n", encoding="utf-8")
    
    res = supervisor.run_replay(file=replay_file)
    assert res["processed"] > 0
    assert res["inserted"] == 1

def test_replay_runner_replaced_records(supervisor, tmp_path) -> None:
    # Trigger line 254: replaced += 1
    replay_file = tmp_path / "replay.jsonl"
    import json as _json
    cap_data = {
        "capture_id": "rep-1",
        "doc_id": "rep-doc-1",
        "title": "Fed rate hike",
        "text": "The Fed hiked rates.",
        "domain": "reuters.com",
    }
    replay_file.write_text(_json.dumps(cap_data) + "\n", encoding="utf-8")
    
    res1 = supervisor.run_replay(file=replay_file)
    assert res1["inserted"] == 1
    assert res1["replaced"] == 0

    replay_file2 = tmp_path / "replay2.jsonl"
    replay_file2.write_text(_json.dumps(cap_data) + "\n", encoding="utf-8")

    res2 = supervisor.run_replay(file=replay_file2)
    assert res2["inserted"] == 0
    assert res2["replaced"] == 1

def test_run_tail_exception_handling(supervisor, monkeypatch) -> None:
    # Trigger line 275-284: run_tail exception handling
    from catchem.awareness_replay import ReplayRunner
    
    def mock_tail(self, handle, poll_seconds, max_per_tick, stop=None):
        cap = build_capture(title="Error story", text="This will raise")
        handle(cap)
        
    monkeypatch.setattr(ReplayRunner, "tail", mock_tail)
    
    def mock_process_capture(cap):
        raise ValueError("mock process_capture error")
    monkeypatch.setattr(supervisor, "process_capture", mock_process_capture)
    
    failure_logged = False
    def mock_record_failure(cap_id, exc_str, text_excerpt):
        nonlocal failure_logged
        failure_logged = True
        assert "mock process_capture error" in exc_str
        
    monkeypatch.setattr(supervisor.storage, "record_failure", mock_record_failure)
    
    supervisor.run_tail()
    assert failure_logged

def test_status_endpoint(supervisor) -> None:
    stat = supervisor.status()
    assert stat["mode"] == supervisor.settings.mode.value
    assert "reviewers" in stat
