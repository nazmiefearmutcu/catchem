"""Tests for the lightweight backtest framework.

The framework is intentionally pure-ish: it takes a supervisor-like object
whose `.storage.reviews_with_pair()` returns (stub_row, deepseek_row)
tuples, and produces a `BacktestRun` dataclass. So we don't need a real
SQLite — a small stand-in object is enough to exercise every branch.

Coverage targets:
    * empty storage => zero-valued summary, empty bins/predictions
    * paired rows with valid scores populate predictions + calibration
    * calibration bins respect the [low, high) ranges (1.0 lands in last bin)
    * sample-size clamps (min 10) so we never call storage with a silly limit
    * error rows + missing scores are filtered out, not crashed on
    * /api/backtest endpoint returns the documented envelope shape
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.backtest import BacktestRun, run_backtest
from catchem.settings import load_settings, reload_settings

# ── stand-in storage / supervisor ──────────────────────────────────────────


class FakeStorage:
    """In-memory `reviews_with_pair` that drops a hard-coded pair list."""

    def __init__(self, pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        self._pairs = list(pairs)
        # Captured for assertions on the call shape.
        self.last_call: tuple[str, str, int] | None = None

    def reviews_with_pair(
        self, reviewer_a: str, reviewer_b: str, limit: int = 500
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        self.last_call = (reviewer_a, reviewer_b, int(limit))
        return list(self._pairs)


class FakeSupervisor:
    """Just exposes the `.storage` attribute the backtest reads."""

    def __init__(self, storage: FakeStorage) -> None:
        self.storage = storage


def _make_pair(
    capture_id: str,
    stub_score: float,
    deepseek_score: float,
    *,
    stub_error: str | None = None,
    deepseek_error: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a (stub_row, deepseek_row) tuple in the storage shape."""
    stub_row = {
        "capture_id": capture_id,
        "reviewer_id": "stub",
        "reviewer_version": "stub-1",
        "created_at": "2026-05-28T12:00:00+00:00",
        "error_code": stub_error,
        "payload": {"finance_relevance_score": stub_score},
    }
    ds_row = {
        "capture_id": capture_id,
        "reviewer_id": "deepseek",
        "reviewer_version": "deepseek-chat",
        "created_at": "2026-05-28T12:00:01+00:00",
        "error_code": deepseek_error,
        "payload": {"finance_relevance_score": deepseek_score},
        "input_tokens": 100,
        "output_tokens": 80,
        "usd_cost": 0.0001,
        "latency_ms": 320,
    }
    return (stub_row, ds_row)


# ── unit tests ─────────────────────────────────────────────────────────────


class TestRunBacktest:
    def test_empty_storage_returns_zero_summary(self) -> None:
        sup = FakeSupervisor(FakeStorage([]))
        run = run_backtest(sup, sample_size=50)
        assert isinstance(run, BacktestRun)
        assert run.items_evaluated == 0
        assert run.relevance_predictions == []
        assert run.calibration_bins == []
        # Zero-valued summary lets the UI render its tiles without branching
        # on null. Every key must exist.
        assert run.summary == {
            "items_evaluated": 0,
            "mean_abs_error": 0.0,
            "mean_signed_error": 0.0,
            "max_abs_error": 0.0,
        }

    def test_paired_reviews_populate_predictions(self) -> None:
        pairs = [
            _make_pair("cap-a", stub_score=0.50, deepseek_score=0.60),
            _make_pair("cap-b", stub_score=0.70, deepseek_score=0.65),
            _make_pair("cap-c", stub_score=0.10, deepseek_score=0.15),
        ]
        sup = FakeSupervisor(FakeStorage(pairs))
        run = run_backtest(sup, sample_size=100)

        assert run.items_evaluated == 3
        # Predictions match input order (newest-first per storage contract).
        ids = [p["capture_id"] for p in run.relevance_predictions]
        assert ids == ["cap-a", "cap-b", "cap-c"]
        # Delta = deepseek - stub for each row.
        deltas = [round(p["delta"], 4) for p in run.relevance_predictions]
        assert deltas == [0.1, -0.05, 0.05]
        # Mean absolute error = average of |delta|.
        assert run.summary["items_evaluated"] == 3
        # 6-decimal rounding (the wire format trims down for stable JSON),
        # so we accept anything within 1e-5 of the analytical mean.
        assert run.summary["mean_abs_error"] == pytest.approx(
            (0.1 + 0.05 + 0.05) / 3, abs=1e-5
        )
        assert run.summary["max_abs_error"] == pytest.approx(0.1, abs=1e-5)

    def test_calibration_bins_respect_ranges(self) -> None:
        # Six rows fanned across the five [low, high) bins, plus a row at
        # exactly 1.0 to verify the last bin is closed on the high side.
        pairs = [
            _make_pair("c0", stub_score=0.05, deepseek_score=0.10),  # 0.0-0.2
            _make_pair("c1", stub_score=0.30, deepseek_score=0.35),  # 0.2-0.4
            _make_pair("c2", stub_score=0.45, deepseek_score=0.55),  # 0.4-0.6
            _make_pair("c3", stub_score=0.65, deepseek_score=0.70),  # 0.6-0.8
            _make_pair("c4", stub_score=0.85, deepseek_score=0.90),  # 0.8-1.0
            _make_pair("c5", stub_score=1.00, deepseek_score=0.95),  # 0.8-1.0 (closed)
        ]
        sup = FakeSupervisor(FakeStorage(pairs))
        run = run_backtest(sup, sample_size=100)

        # All five bins present (because we seeded one row per bin), and the
        # last bin holds two rows because 1.0 landed in [0.8, 1.0].
        assert len(run.calibration_bins) == 5
        counts = {(b["bin_low"], b["bin_high"]): b["predicted_count"] for b in run.calibration_bins}
        assert counts[(0.0, 0.2)] == 1
        assert counts[(0.2, 0.4)] == 1
        assert counts[(0.4, 0.6)] == 1
        assert counts[(0.6, 0.8)] == 1
        assert counts[(0.8, 1.0)] == 2

        # Calibration gap = avg(ground_truth) - avg(predicted) within bin.
        # Last bin: predicted avg = (0.85 + 1.00)/2 = 0.925
        #           ground_truth avg = (0.90 + 0.95)/2 = 0.925
        #           gap should be ~0.
        last = next(b for b in run.calibration_bins if b["bin_low"] == 0.8)
        assert last["calibration_gap"] == pytest.approx(0.0, abs=1e-6)

    def test_error_rows_are_filtered(self) -> None:
        # Mix of valid + errored pairs. Errored ones should disappear from
        # the result entirely (they don't carry a usable score).
        pairs = [
            _make_pair("good-a", stub_score=0.5, deepseek_score=0.6),
            _make_pair(
                "ds-erred", stub_score=0.5, deepseek_score=0.0, deepseek_error="timeout"
            ),
            _make_pair(
                "stub-erred", stub_score=0.0, deepseek_score=0.5, stub_error="bad_json"
            ),
            _make_pair("good-b", stub_score=0.3, deepseek_score=0.4),
        ]
        sup = FakeSupervisor(FakeStorage(pairs))
        run = run_backtest(sup, sample_size=100)
        ids = [p["capture_id"] for p in run.relevance_predictions]
        assert ids == ["good-a", "good-b"]
        assert run.items_evaluated == 2

    def test_missing_scores_are_filtered(self) -> None:
        # Pairs where either payload lacks a `finance_relevance_score` —
        # treated identically to errors: dropped silently, no crash.
        stub_no_score = {
            "capture_id": "x",
            "error_code": None,
            "payload": {},  # missing field
        }
        ds_ok = {
            "capture_id": "x",
            "error_code": None,
            "payload": {"finance_relevance_score": 0.5},
        }
        pairs = [
            (stub_no_score, ds_ok),
            _make_pair("y", stub_score=0.2, deepseek_score=0.3),
        ]
        sup = FakeSupervisor(FakeStorage(pairs))
        run = run_backtest(sup, sample_size=100)
        assert run.items_evaluated == 1
        assert run.relevance_predictions[0]["capture_id"] == "y"

    def test_sample_size_clamps_to_minimum(self) -> None:
        # Even if a caller passes 1 we want at least 10 — preserves a
        # meaningful evaluation window. Tests the float=>int path too.
        sup = FakeSupervisor(FakeStorage([]))
        run_backtest(sup, sample_size=1)  # type: ignore[arg-type]
        assert sup.storage.last_call is not None
        _, _, limit = sup.storage.last_call
        assert limit == 10

    def test_predictions_sample_capped_at_50(self) -> None:
        # 60 input pairs — sample should cap at 50 even though all are valid,
        # while `items_evaluated` reflects the full N.
        pairs = [
            _make_pair(f"cap-{i:02d}", stub_score=0.4 + (i % 5) * 0.02,
                       deepseek_score=0.5 + (i % 5) * 0.02)
            for i in range(60)
        ]
        sup = FakeSupervisor(FakeStorage(pairs))
        run = run_backtest(sup, sample_size=200)
        assert run.items_evaluated == 60
        assert len(run.relevance_predictions) == 50


# ── endpoint contract test ─────────────────────────────────────────────────


def test_api_backtest_endpoint_returns_documented_envelope(tmp_path, monkeypatch) -> None:
    """`GET /api/backtest` must return the schema the UI depends on, even
    when the database is empty (zero summary, empty bins/predictions)."""
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()

    app = create_app(load_settings())
    client = TestClient(app)
    # Lifespan boots the supervisor that the endpoint depends on.
    with client:
        res = client.get("/api/backtest?sample_size=50")
        assert res.status_code == 200, res.text
        body = res.json()

    # Envelope shape — bump schema_version in backtest.py if any of these change.
    assert body["schema_version"] == 1
    assert "ran_at" in body
    assert body["sample_size"] == 50
    assert body["summary"] == {
        "items_evaluated": 0,
        "mean_abs_error": 0.0,
        "mean_signed_error": 0.0,
        "max_abs_error": 0.0,
    }
    assert body["calibration_bins"] == []
    assert body["predictions_sample"] == []
