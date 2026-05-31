"""v61: news persistence signal — long-running stories.

Pins compute_persistence() pure-logic contract + the HTTP endpoint envelope.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from catchem.quant.persistence import compute_persistence

UTC = UTC


def _record(day_offset_from_today: int, asset_class: str = "equities", symbol: str = "AAPL", title: str = "") -> dict:
    """Build a record with a published_ts shifted N days back from today."""
    ts = datetime.now(UTC) - timedelta(days=day_offset_from_today)
    return {
        "capture_id": f"r-{day_offset_from_today}-{asset_class}-{symbol}",
        "published_ts": ts.isoformat().replace("+00:00", "Z"),
        "asset_classes": [asset_class],
        "candidate_symbols": [symbol],
        "title": title or f"{symbol} story on day -{day_offset_from_today}",
    }


def test_compute_persistence_empty_input() -> None:
    assert compute_persistence([]) == []


def test_compute_persistence_single_day_low_ratio() -> None:
    """3 records on the same day → 1 day covered, ratio 1/7 ≈ 0.143."""
    records = [
        _record(0, symbol="AAPL", title="Apple a"),
        _record(0, symbol="AAPL", title="Apple b"),
        _record(0, symbol="AAPL", title="Apple c"),
    ]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out) == 1
    assert out[0].days_covered == 1
    assert out[0].total_records == 3
    assert abs(out[0].persistence_ratio - 1 / 7) < 1e-9


def test_compute_persistence_filters_min_records() -> None:
    """Scope with fewer than min_records is dropped entirely."""
    records = [_record(0, symbol="AAPL"), _record(1, symbol="AAPL")]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert out == []


def test_compute_persistence_high_ratio_persistent_story() -> None:
    """Records spanning 5 distinct days → ratio 5/7."""
    records = [_record(d, symbol="BTC-USD") for d in range(5)]  # days -0..-4
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out) == 1
    assert out[0].days_covered == 5
    assert abs(out[0].persistence_ratio - 5 / 7) < 1e-9


def test_compute_persistence_sample_titles_capped_at_3() -> None:
    records = [_record(d, symbol="AAPL", title=f"t{d}") for d in range(6)]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out[0].sample_titles) == 3


def test_compute_persistence_sorts_by_ratio_desc() -> None:
    """Higher days_covered ratio comes first; ties break on total_records."""
    records = (
        [_record(d, asset_class="rates", symbol="UST") for d in range(2)]
        + [_record(d, asset_class="equities", symbol="AAPL") for d in range(5)]
    )
    out = compute_persistence(records, window_days=7, min_records=2)
    # Apple (5 days) should beat UST (2 days)
    assert out[0].scope.startswith("equities/AAPL")
    assert out[0].days_covered > out[1].days_covered


def test_compute_persistence_skips_bad_timestamps() -> None:
    """Records with no/bad timestamp drop silently — no crash."""
    records = [
        _record(0, symbol="AAPL"),
        {"capture_id": "bad", "asset_classes": ["x"], "candidate_symbols": ["x"]},
        {"capture_id": "bad2", "published_ts": "garbage", "asset_classes": ["x"]},
    ]
    out = compute_persistence(records, window_days=7, min_records=1)
    # Only the AAPL one is included
    assert all(b.scope.startswith("equities/AAPL") for b in out)


def test_compute_persistence_window_days_below_one_is_clamped() -> None:
    """``window_days < 1`` clamps to 1 so the ratio denominator stays >=1 (line 67-68).

    Three same-day records with ``window_days=0`` must not divide by zero;
    after clamping, ratio = 1 day / 1 = 1.0.
    """

    records = [_record(0, symbol="AAPL", title=f"t{i}") for i in range(3)]
    out = compute_persistence(records, window_days=0, min_records=3)
    assert len(out) == 1
    assert out[0].days_covered == 1
    assert out[0].persistence_ratio == pytest.approx(1.0)


def test_compute_persistence_coerces_scalar_asset_class_and_symbols() -> None:
    """Non-list ``asset_classes`` / ``candidate_symbols`` are wrapped (line 98-101).

    A record can arrive with scalar string fields instead of lists; the
    signal must coerce them rather than crash, producing a single
    ``"<asset_class>/<symbol>"`` scope.
    """

    ts = (datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    records = [
        {
            "capture_id": f"scalar-{i}",
            "published_ts": ts,
            "asset_classes": "equities",  # scalar, not a list
            "candidate_symbols": "TSLA",  # scalar, not a list
            "title": f"Tesla {i}",
        }
        for i in range(3)
    ]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out) == 1
    assert out[0].scope == "equities/TSLA"
    assert out[0].total_records == 3


def test_compute_persistence_missing_asset_class_uses_em_dash_scope() -> None:
    """Empty ``asset_classes`` falls back to the ``"—"`` placeholder (line 103-104).

    A record with symbols but no asset class still buckets under
    ``"—/<symbol>"`` so incomplete records aren't silently dropped.
    """

    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    records = [
        {
            "capture_id": f"noac-{i}",
            "published_ts": ts,
            "asset_classes": [],
            "candidate_symbols": ["GME"],
            "title": f"GME {i}",
        }
        for i in range(3)
    ]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out) == 1
    assert out[0].scope == "—/GME"


def test_compute_persistence_records_without_titles_yield_empty_sample_list() -> None:
    """Records carrying no ``title`` append nothing to ``sample_titles`` (line 110-111).

    The sample-title slot is only filled when a non-empty title exists; an
    untitled persistent scope must still be reported, just with an empty
    ``sample_titles`` list rather than ``["", "", ""]``.
    """

    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    records = [
        {
            "capture_id": f"untitled-{i}",
            "published_ts": ts,
            "asset_classes": ["equities"],
            "candidate_symbols": ["AAPL"],
            # no "title" key at all
        }
        for i in range(3)
    ]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out) == 1
    assert out[0].sample_titles == []
    assert out[0].total_records == 3


def test_compute_persistence_missing_symbols_uses_em_dash_top_symbol() -> None:
    """No ``candidate_symbols`` ⇒ top symbol is ``"—"`` (line 102 else branch)."""

    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    records = [
        {
            "capture_id": f"nosym-{i}",
            "published_ts": ts,
            "asset_classes": ["macro"],
            "candidate_symbols": [],
            "title": f"macro {i}",
        }
        for i in range(3)
    ]
    out = compute_persistence(records, window_days=7, min_records=3)
    assert len(out) == 1
    assert out[0].scope == "macro/—"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    from catchem.api import create_app
    from catchem.settings import load_settings, reload_settings
    reload_settings()
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


def test_persistence_endpoint_returns_envelope(client) -> None:
    resp = client.get("/api/quant/persistence?limit=100&window_days=7")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("schema_version", "generated_at", "limit", "window_days", "min_records", "buckets"):
        assert key in body
    assert body["window_days"] == 7
    assert isinstance(body["buckets"], list)


def test_persistence_endpoint_rejects_invalid_params(client) -> None:
    assert client.get("/api/quant/persistence?window_days=0").status_code == 422
    assert client.get("/api/quant/persistence?window_days=999").status_code == 422
    assert client.get("/api/quant/persistence?top_n=0").status_code == 422
