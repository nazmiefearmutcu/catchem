import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catchem.schemas import AwarenessCaptureView
from catchem.settings import Settings
from catchem.storage import Storage
from catchem.supervisor import Supervisor


@pytest.fixture
def temp_storage(tmp_path: Path) -> Storage:
    db_path = tmp_path / "data" / "catchem.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(
        db_path=db_path,
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
    )
    yield storage
    storage.close()


def test_storage_queue_operations(temp_storage: Storage):
    # Initial queue count should be 0
    assert temp_storage.queue_count() == 0

    # Enqueue a capture
    temp_storage.enqueue_capture("test-id-1", '{"capture_id": "test-id-1", "text": "hello"}')
    assert temp_storage.queue_count() == 1

    # Enqueue another
    temp_storage.enqueue_capture("test-id-2", '{"capture_id": "test-id-2", "text": "world"}')
    assert temp_storage.queue_count() == 2

    # Dequeue captures with limit 1
    dequeued = temp_storage.dequeue_captures(limit=1)
    assert len(dequeued) == 1
    queue_id, capture_id, payload = dequeued[0]
    assert capture_id == "test-id-1"
    assert payload == '{"capture_id": "test-id-1", "text": "hello"}'

    # Dequeue with limit 2
    all_dequeued = temp_storage.dequeue_captures(limit=2)
    assert len(all_dequeued) == 2
    assert all_dequeued[0][1] == "test-id-1"
    assert all_dequeued[1][1] == "test-id-2"

    # Ack the first capture and verify count drops to 1
    temp_storage.ack_capture(queue_id)
    assert temp_storage.queue_count() == 1

    # Remaining in queue should be test-id-2
    remaining = temp_storage.dequeue_captures(limit=1)
    assert len(remaining) == 1
    assert remaining[0][1] == "test-id-2"

    # Ack the second one
    temp_storage.ack_capture(remaining[0][0])
    assert temp_storage.queue_count() == 0


def test_supervisor_enqueue_and_worker_success(tmp_path: Path):
    settings = Settings()
    settings.paths.catchem_output_dir = tmp_path
    settings.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    settings.live.ingestion_queue_enabled = True

    # Build supervisor
    sup = Supervisor(settings)
    assert sup._queue_thread is not None
    assert sup._queue_thread.is_alive()

    # Create dummy capture
    cap = AwarenessCaptureView(
        capture_id="cap-queue-test",
        doc_id="doc-queue-test",
        text="This is a test of the persistent queue.",
        title="Persistent Queue Test",
        url="https://example.com/queue-test",
        source_type="ws"
    )

    try:
        # Calling process_capture should enqueue it and return None
        res = sup.process_capture(cap)
        assert res is None

        # Give the background thread some time to dequeue and process
        start_time = time.time()
        processed = False
        while time.time() - start_time < 5.0:
            if sup.storage.queue_count() == 0 and sup.storage.get_record("cap-queue-test") is not None:
                processed = True
                break
            time.sleep(0.1)

        assert processed, "Capture was not processed by the queue worker thread"

        # Verify the record exists in the DB
        rec = sup.storage.get_record("cap-queue-test")
        assert rec is not None
        assert rec["title"] == "Persistent Queue Test"
    finally:
        sup.close()


def test_supervisor_queue_worker_processing_failure(tmp_path: Path):
    settings = Settings()
    settings.paths.catchem_output_dir = tmp_path
    settings.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    settings.live.ingestion_queue_enabled = True

    sup = Supervisor(settings)
    assert sup._queue_thread is not None

    # Create capture with invalid JSON payload or trigger processing exception
    cap = AwarenessCaptureView(
        capture_id="cap-fail-test",
        doc_id="doc-fail-test",
        text="Force failure in processing",
        title="Failure Test",
        url="https://example.com/fail-test",
        source_type="ws"
    )

    # Mock self.service.process to raise an exception when this capture is run
    sup.service.process = MagicMock(side_effect=ValueError("Simulated processing error"))

    try:
        # Enqueue
        sup.process_capture(cap)

        # Wait for worker to consume it
        start_time = time.time()
        failed_logged = False
        while time.time() - start_time < 5.0:
            if sup.storage.queue_count() == 0 and sup.storage.dlq_count() > 0:
                failed_logged = True
                break
            time.sleep(0.1)

        assert failed_logged, "Failed capture was not logged to DLQ"
        assert sup.storage.queue_count() == 0
    finally:
        sup.close()


def test_supervisor_enqueue_failure_sync_fallback(tmp_path: Path):
    settings = Settings()
    settings.paths.catchem_output_dir = tmp_path
    settings.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    settings.live.ingestion_queue_enabled = True

    sup = Supervisor(settings)

    # Mock storage.enqueue_capture to throw an exception
    sup.storage.enqueue_capture = MagicMock(side_effect=Exception("Database lock error"))

    cap = AwarenessCaptureView(
        capture_id="cap-fallback-test",
        doc_id="doc-fallback-test",
        text="Testing fallback sync path",
        title="Fallback Sync Test",
        url="https://example.com/fallback-test",
        source_type="ws"
    )

    try:
        # Calling process_capture should raise exception in enqueue,
        # trigger catch, and execute sync processing directly returning the record.
        rec = sup.process_capture(cap)
        assert rec is not None
        assert rec.capture_id == "cap-fallback-test"
        assert sup.storage.get_record("cap-fallback-test") is not None
    finally:
        sup.close()


def test_supervisor_queue_worker_loop_general_exception(tmp_path: Path):
    settings = Settings()
    settings.paths.catchem_output_dir = tmp_path
    settings.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    settings.live.ingestion_queue_enabled = True

    sup = Supervisor(settings)

    # Mock storage.dequeue_captures to raise an exception once to cover the loop exception catch path
    original_dequeue = sup.storage.dequeue_captures
    dequeue_mock = MagicMock(side_effect=[Exception("Transient dequeue error"), []])
    sup.storage.dequeue_captures = dequeue_mock

    try:
        # Give the thread a moment to loop and hit the mock exception
        time.sleep(0.5)
        # Restore original so it doesn't infinite loop on exceptions
        sup.storage.dequeue_captures = original_dequeue
    finally:
        sup.close()


def test_supervisor_queue_worker_dlq_failure(tmp_path: Path):
    settings = Settings()
    settings.paths.catchem_output_dir = tmp_path
    settings.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    settings.live.ingestion_queue_enabled = True

    sup = Supervisor(settings)

    # Mock both process AND record_failure to fail
    sup.service.process = MagicMock(side_effect=ValueError("Process error"))
    sup.storage.record_failure = MagicMock(side_effect=RuntimeError("DLQ write error"))

    cap = AwarenessCaptureView(
        capture_id="cap-dlq-fail",
        doc_id="doc-dlq-fail",
        text="Double failure test",
        title="Double Failure",
        url="https://example.com/double-fail",
        source_type="ws"
    )

    try:
        # Enqueue
        sup.process_capture(cap)

        # Wait for worker to consume it
        start_time = time.time()
        while time.time() - start_time < 5.0:
            if sup.storage.queue_count() == 0:
                break
            time.sleep(0.1)

        assert sup.storage.queue_count() == 0
    finally:
        sup.close()

