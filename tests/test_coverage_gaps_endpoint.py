"""Contract tests for the awareness blind-spot endpoint.

``GET /api/news/coverage-gaps`` answers the inverted firehose question: with
hundreds of sources arriving, *which watched terms have NO recent coverage?*

Pattern mirrors ``tests/test_top_recent_endpoint.py`` /
``tests/test_quant_diagnostics_api.py``: build the app via ``create_app`` under
a real lifespan with an isolated temp DB, seed storage through the live
supervisor via ``POST /process-one``, then assert the envelope + the
covered/gap split.

Pins:
  * full envelope (schema_version + generated_at + the find_coverage_gaps dict),
  * a never-mentioned watch term lands in ``gaps``,
  * a mentioned watch term lands in ``covered`` with a positive mention_count
    and a non-negative freshness age,
  * the default mega-cap watchlist is used when ``priority_tickers`` is empty,
  * the degraded (no-supervisor) path still returns 200 with empty lists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App over a real lifespan with an isolated DB + background tasks off."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    reload_settings()
    return TestClient(create_app(load_settings()))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Watchlist = exactly AAPL (will be covered) + ZZZZ (guaranteed gap)."""
    monkeypatch.setenv("CATCHEM_NEWS__PRIORITY_TICKERS", '["AAPL","ZZZZ"]')
    with _make_client(tmp_path, monkeypatch) as c:
        yield c


@pytest.fixture
def seeded_client(client: TestClient, synth_capture) -> TestClient:
    """Two finance-relevant records: one mentions AAPL, one (Fed) does not."""
    cap_fed = synth_capture(capture_id="cg-fed", doc_id="cg-d1")
    cap_aapl = synth_capture(
        capture_id="cg-aapl",
        doc_id="cg-d2",
        title="Apple beats earnings and raises full-year guidance",
        text=(
            "Apple Inc reported revenue above consensus and raised guidance. "
            "$AAPL rose 4% in after-hours trading on the news."
        ),
        domain="wsj.com",
    )
    for cap in (cap_fed, cap_aapl):
        r = client.post("/process-one", json=cap.model_dump(mode="json"))
        assert r.status_code == 200, r.text
    return client


# ── Envelope ──────────────────────────────────────────────────────────────────


def test_coverage_gaps_envelope_shape(seeded_client: TestClient) -> None:
    r = seeded_client.get("/api/news/coverage-gaps")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "schema_version",
        "generated_at",
        "window_seconds",
        "covered",
        "gaps",
    ):
        assert key in body, f"missing {key}"
    assert body["schema_version"] == 1
    assert isinstance(body["generated_at"], str) and body["generated_at"]
    assert isinstance(body["window_seconds"], (int, float))
    assert isinstance(body["covered"], list)
    assert isinstance(body["gaps"], list)


# ── covered vs gap split on seeded data ───────────────────────────────────────


def test_mentioned_term_covered_unmentioned_is_gap(seeded_client: TestClient) -> None:
    r = seeded_client.get("/api/news/coverage-gaps?window_seconds=86400")
    assert r.status_code == 200, r.text
    body = r.json()

    # ZZZZ is never in any record → blind spot.
    assert "ZZZZ" in body["gaps"]

    # AAPL appears (title + $AAPL cashtag → candidate_symbols) → covered.
    covered_terms = {c["term"] for c in body["covered"]}
    assert "AAPL" in covered_terms
    assert "AAPL" not in body["gaps"]

    aapl = next(c for c in body["covered"] if c["term"] == "AAPL")
    assert aapl["mention_count"] >= 1
    assert aapl["last_seen_age_seconds"] is not None
    assert aapl["last_seen_age_seconds"] >= 0.0


def test_tight_window_pushes_everything_into_gaps(seeded_client: TestClient) -> None:
    """A sub-second window is narrower than the seed→request latency, so even
    the mentioned term has no *in-window* coverage and becomes a gap."""
    r = seeded_client.get("/api/news/coverage-gaps?window_seconds=0.001")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["covered"] == []
    assert set(body["gaps"]) == {"AAPL", "ZZZZ"}


# ── default watchlist fallback ────────────────────────────────────────────────


def test_default_watchlist_used_when_priority_tickers_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_capture
) -> None:
    """No configured priority_tickers → endpoint falls back to a mega-cap set,
    so every term is accounted for as either covered or a gap (never empty)."""
    monkeypatch.delenv("CATCHEM_NEWS__PRIORITY_TICKERS", raising=False)
    with _make_client(tmp_path, monkeypatch) as c:
        cap = synth_capture(
            capture_id="cg-def",
            doc_id="cg-def1",
            title="Apple (AAPL) climbs to a record",
            text="$AAPL leads the megacaps higher.",
            domain="wsj.com",
        )
        assert c.post("/process-one", json=cap.model_dump(mode="json")).status_code == 200

        body = c.get("/api/news/coverage-gaps?window_seconds=86400").json()
        all_terms = {x["term"] for x in body["covered"]} | set(body["gaps"])
        # The built-in mega-cap fallback includes AAPL — and AAPL was seeded.
        assert "AAPL" in all_terms
        assert "AAPL" in {x["term"] for x in body["covered"]}
        # Fallback set is non-trivial (several megacaps), so the union is > 1.
        assert len(all_terms) > 1


# ── degraded path (no supervisor) ─────────────────────────────────────────────


def test_degraded_without_supervisor_returns_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built without entering the lifespan → no supervisor. Endpoint must
    still answer 200 with empty covered/gaps rather than a 503."""
    import catchem.api as api_module

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    monkeypatch.setenv("CATCHEM_NEWS__PRIORITY_TICKERS", "[]")
    monkeypatch.setattr(api_module, "_SUPERVISOR", None, raising=False)
    reload_settings()
    app = create_app(load_settings())

    # No `with` → lifespan never runs → _SUPERVISOR stays None.
    plain = TestClient(app)
    r = plain.get("/api/news/coverage-gaps")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 1
    # Degraded: zero records → every watch term (default set) is a gap, none
    # covered. The contract the UI relies on is simply: covered is empty.
    assert body["covered"] == []
    assert isinstance(body["gaps"], list)
