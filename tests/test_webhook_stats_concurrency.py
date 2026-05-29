"""webhook_stats counters are thread-safe under concurrent increments (v80 fix).

`webhook_stats[key] += 1` is a non-atomic read-modify-write executed from
several threads (the ingest workers bump `attempted`; the _webhook_pool workers
bump sent/filtered/failed). Without a lock, concurrent increments silently drop
counts. This pins that the lock makes the counts exact, and that the snapshot
accessor returns an independent copy.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "true")
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")

    from catchem.settings import load_settings, reload_settings
    from catchem.supervisor import Supervisor

    reload_settings()
    sup = Supervisor(load_settings())
    yield sup
    sup.close()


def test_webhook_runner_counter_has_no_lost_updates(supervisor, monkeypatch) -> None:
    import catchem.supervisor as sup_mod

    # Stub the network so _webhook_runner only classifies + increments.
    monkeypatch.setattr(sup_mod, "send_webhook", lambda record, cfg: (True, "ok"))

    threads_n, per_thread = 8, 1000
    total = threads_n * per_thread

    def _hammer() -> None:
        for _ in range(per_thread):
            supervisor._webhook_runner({"capture_id": "c"})

    threads = [threading.Thread(target=_hammer) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = supervisor.webhook_stats_snapshot()
    assert snap["sent"] == total, f"lost-update race: expected {total}, got {snap['sent']}"
    assert snap["filtered"] == 0
    assert snap["failed"] == 0


def test_webhook_stats_snapshot_is_an_independent_copy(supervisor) -> None:
    snap = supervisor.webhook_stats_snapshot()
    snap["sent"] = 999  # mutating the snapshot must not reach the live dict
    assert supervisor.webhook_stats["sent"] == 0
