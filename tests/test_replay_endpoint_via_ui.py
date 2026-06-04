"""POST /replay — Round 7 R3 contract regression.

The /replay endpoint has existed since the supervisor was first wired, but
no UI surface called it until the Replay tab was added in Round 7. This
test pins the contract the new tab depends on:

  * 200 with {processed:int, skipped:int} on a normal run
  * Idempotent — re-running over the same captures bumps `skipped`, not `processed`
  * Empty awareness dir → {processed:0, skipped:0} (no 5xx, no key drift)
  * `max_records` is honored as the upper bound on the scan
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import catchem.api as api_module
from catchem.api import create_app
from catchem.demo import build_capture, write_jsonl
from catchem.schemas import AwarenessCaptureView
from catchem.settings import load_settings, reload_settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    out = tmp_path / "data"
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(out))
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(out))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def _write_capture(awareness_root: Path, text: str, url: str) -> str:
    cap = build_capture(title="t", text=text, url=url, published_ts=datetime.now(UTC))
    write_jsonl(cap, awareness_root)
    return cap.capture_id


def test_replay_empty_dir_returns_zero_zero(client: TestClient) -> None:
    """An empty Awareness dir must not 5xx — supervisor should report zeros."""
    r = client.post("/replay", json={"max_records": 50})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"processed", "skipped"}
    assert isinstance(body["processed"], int)
    assert isinstance(body["skipped"], int)
    assert body["processed"] == 0
    assert body["skipped"] == 0


def test_replay_processes_a_real_capture_then_skips_on_replay(
    client: TestClient, tmp_path: Path,
) -> None:
    """First run processes, second run over the same JSONL skips — pins idempotency."""
    # Capture written under the configured awareness dir, which the fixture
    # pointed at the catchem output root.
    aware_root = tmp_path / "data"
    cap_id = _write_capture(
        aware_root,
        text=(
            "The Federal Reserve raised its benchmark interest rate by 25 basis "
            "points on Wednesday. Apple (AAPL) and Microsoft (MSFT) both fell."
        ),
        url="https://example.com/fed-25bps",
    )

    r1 = client.post("/replay", json={"max_records": 50})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["processed"] >= 1, body1
    # Storage now holds the capture.
    r_rec = client.get(f"/record/{cap_id}")
    assert r_rec.status_code == 200, r_rec.text

    # Second pass over the same JSONL — supervisor MUST skip via persisted offsets.
    r2 = client.post("/replay", json={"max_records": 50})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    # On the second pass either nothing is reprocessed (offset honored) or it's
    # counted as a skip. Either way we must not see another new processed row
    # for the same capture id.
    assert body2["processed"] == 0, body2


def test_replay_honors_max_records_clamp(client: TestClient, tmp_path: Path) -> None:
    """Writing 3 captures and asking for max=1 must process at most one."""
    aware_root = tmp_path / "data"
    for i in range(3):
        _write_capture(
            aware_root,
            text=f"Fed raised rates by 25 bps (paragraph {i}). Apple (AAPL) reacted.",
            url=f"https://example.com/fed-{i}",
        )

    r = client.post("/replay", json={"max_records": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] <= 1, body


def test_replay_rejects_garbage_payload(client: TestClient) -> None:
    """A non-int max_records value must surface as 4xx, not a 5xx crash."""
    r = client.post("/replay", json={"max_records": "fifty"})
    assert r.status_code in (400, 422), r.text


def test_replay_default_body_uses_50(client: TestClient) -> None:
    """Empty body — embedded default of 50 takes effect, no validation error."""
    # The handler signature is `max_records: int = Body(50, embed=True)`,
    # so an empty {} should be valid.
    r = client.post("/replay", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "processed" in body
    assert "skipped" in body


def test_replay_endpoint_exposes_truthful_storage_and_dlq_fields(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    aware_root = tmp_path / "data"
    _write_capture(
        aware_root,
        text="Fed raised rates by 25 bps and Apple (AAPL) reacted.",
        url="https://example.com/fed-truth",
    )

    r1 = client.post("/replay", json={"max_records": 50})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["processed"] >= 1, body1
    assert body1["failed"] == 0
    assert body1["dlq_delta"] == 0
    assert body1["inserted"] >= 1
    assert body1["net_new_records"] >= 1
    assert body1["records_after"]["total"] >= body1["records_before"]["total"]

    _write_capture(
        aware_root,
        text="This capture will fail inside the service.",
        url="https://example.com/fed-fail",
    )

    def boom(_cap: AwarenessCaptureView):
        raise RuntimeError("synthetic process failure")

    assert api_module._SUPERVISOR is not None
    monkeypatch.setattr(api_module._SUPERVISOR.service, "process", boom)

    r2 = client.post("/replay", json={"max_records": 50})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["processed"] == 0, body2
    assert body2["skipped"] == 1, body2
    assert body2["failed"] == 1, body2
    assert body2["dlq_delta"] == 1, body2
    assert body2["inserted"] == 0, body2
    assert body2["net_new_records"] == 0, body2


def test_replay_rejects_negative_max_records(client: TestClient) -> None:
    """`max_records=-1` MUST surface a 422 — pre-fix the int was passed
    straight through and the supervisor silently processed one row before
    the loop short-circuited, leaking nonsense values into observability.
    """
    r = client.post("/replay", json={"max_records": -1})
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


def test_replay_rejects_huge_max_records(client: TestClient) -> None:
    """Bound the upper end so an operator typo (e.g. 99999999) can't
    schedule an open-ended replay that blocks the loop for minutes.
    """
    r = client.post("/replay", json={"max_records": 10_000_000})
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


def test_replay_rejects_zero_max_records(client: TestClient) -> None:
    """`max_records=0` MUST 422. The supervisor's short-circuit is
    `if max_records and processed >= max_records`, so 0 falls through and
    iterates EVERY awareness file (treated as "no cap"). On a populated
    awareness dir that blocks the loop for minutes. Reject at the
    boundary so the dangerous value never reaches the supervisor.
    """
    r = client.post("/replay", json={"max_records": 0})
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
