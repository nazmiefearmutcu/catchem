"""End-to-end concurrency probe for the ingest hot path (v80 audit follow-up).

The earlier audit reasoned that `Supervisor.process_capture` is safe under the
news poller's 4-thread ingest (asyncio.to_thread) + the WS-push reader: the
stages are stateless, storage is lock-guarded, and (after the v80 fix) the
VectorIndex cache + on-disk publish are too. This test EXECUTES that scenario
end-to-end on a real assembled Supervisor (CPU stubs, temp SQLite + vector
dir) — not unit mocks — so the pieces are proven to compose correctly under
contention, not just individually.

Asserts:
  * no thread raises (the composed pipeline is crash-free under 4 workers),
  * every distinct capture lands exactly once,
  * a concurrent RE-ingest of the same capture_ids upserts (no duplicate rows).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from catchem.demo import build_capture
from catchem.settings import load_settings, reload_settings
from catchem.supervisor import Supervisor

N = 120  # comfortably exercises the 4-worker pool without being slow


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "true")  # CPU-only, fast
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    reload_settings()
    return Supervisor(load_settings())


def _make_caps(n: int) -> list:
    base = datetime.now(UTC) - timedelta(hours=2)
    return [
        build_capture(
            title=f"Story {i}: Federal Reserve signals a rate move",
            text="The Fed weighed interest-rate policy amid inflation and earnings data.",
            domain="reuters.com",
            url=f"https://example.com/concurrent-{i}",
            published_ts=base + timedelta(seconds=i),
        )
        for i in range(n)
    ]


def test_concurrent_process_capture_is_thread_safe_end_to_end(supervisor) -> None:
    caps = _make_caps(N)
    errors: list[BaseException] = []

    def _ingest(cap) -> None:
        try:
            supervisor.process_capture(cap)
        except BaseException as exc:  # capture ANY error raised on a worker
            errors.append(exc)

    # Mirror the poller's bounded-concurrency ingest (4 worker threads).
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_ingest, caps))
    supervisor.storage.flush()

    assert not errors, f"concurrent process_capture raised: {errors[:3]}"
    assert supervisor.storage.count_records()["total"] == N, "every distinct capture must land once"

    # Re-ingest the SAME captures concurrently — the deterministic capture_id +
    # INSERT OR REPLACE must upsert, never duplicate.
    errors.clear()
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_ingest, caps))
    supervisor.storage.flush()

    assert not errors, f"concurrent re-ingest raised: {errors[:3]}"
    assert supervisor.storage.count_records()["total"] == N, "re-ingest must upsert, not duplicate"
