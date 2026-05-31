"""Tests for ``catchem.quant.arrival_heatmap``.

Covers the contract:
  * empty input → all 168 cells exist with count=0
  * records spread across days/hours populate the right cells
  * ``max_count``, ``peak_cells`` and ``total_samples`` are correct
  * timezone parameter actually shifts which (weekday, hour) the record
    lands in (UTC vs ET vs Asia/Tokyo)
  * malformed / missing timestamps are silently dropped
  * invalid timezone string falls back to America/New_York
  * canonical cell ordering: row-major, weekday outer, hour inner
"""

from __future__ import annotations

from catchem.quant.arrival_heatmap import (
    WEEKDAY_LABELS,
    compute_heatmap,
)


def _rec(ts: str) -> dict:
    return {"published_ts": ts}


# ── shape / empty ────────────────────────────────────────────────────────


def test_empty_records_yields_168_zero_cells_in_canonical_order() -> None:
    out = compute_heatmap([])
    cells = out["cells"]
    assert len(cells) == 168
    # row-major: weekday outer, hour inner
    for idx, cell in enumerate(cells):
        assert cell["weekday"] == idx // 24
        assert cell["hour"] == idx % 24
        assert cell["count"] == 0
    assert out["max_count"] == 0
    assert out["total_samples"] == 0
    assert out["peak_cells"] == []
    assert out["weekday_labels"] == list(WEEKDAY_LABELS)
    assert out["timezone"] == "America/New_York"


# ── happy path ───────────────────────────────────────────────────────────


def test_records_populate_correct_cells_in_default_et_timezone() -> None:
    # 2026-05-27 is a Wednesday (weekday=2). In May ET is UTC-4.
    # 14:00 UTC == 10:00 ET → (2, 10)
    # 14:30 UTC == 10:30 ET → (2, 10) — same cell as above
    # 21:00 UTC == 17:00 ET → (2, 17)
    records = [
        _rec("2026-05-27T14:00:00Z"),
        _rec("2026-05-27T14:30:00Z"),
        _rec("2026-05-27T21:00:00Z"),
    ]
    out = compute_heatmap(records)
    by_key = {(c["weekday"], c["hour"]): c["count"] for c in out["cells"]}
    assert by_key[(2, 10)] == 2
    assert by_key[(2, 17)] == 1
    assert out["total_samples"] == 3
    assert out["max_count"] == 2
    assert out["peak_cells"] == [{"weekday": 2, "hour": 10, "count": 2}]


def test_multiple_cells_tied_for_max_all_listed() -> None:
    # Two distinct cells both get 3 hits → peak_cells has both.
    records = (
        [_rec("2026-05-27T14:00:00Z")] * 3  # Wed 10:00 ET
        + [_rec("2026-05-28T19:00:00Z")] * 3  # Thu 15:00 ET
        + [_rec("2026-05-29T13:00:00Z")] * 1  # Fri 09:00 ET
    )
    out = compute_heatmap(records)
    assert out["max_count"] == 3
    peaks = {(c["weekday"], c["hour"]) for c in out["peak_cells"]}
    assert peaks == {(2, 10), (3, 15)}
    assert out["total_samples"] == 7


# ── timezone behaviour ───────────────────────────────────────────────────


def test_timezone_shifts_which_cell_record_lands_in() -> None:
    # 2026-05-28T00:30:00Z (Thursday 00:30 UTC):
    #   UTC:        Thu (weekday=3) hour 0
    #   ET (UTC-4): Wed (weekday=2) hour 20
    #   Tokyo (+9): Thu (weekday=3) hour 9
    ts = "2026-05-28T00:30:00Z"
    utc = compute_heatmap([_rec(ts)], timezone="UTC")
    et = compute_heatmap([_rec(ts)], timezone="America/New_York")
    tk = compute_heatmap([_rec(ts)], timezone="Asia/Tokyo")
    assert utc["timezone"] == "UTC"
    assert et["timezone"] == "America/New_York"
    assert tk["timezone"] == "Asia/Tokyo"
    def by_key(out: dict) -> set[tuple[int, int]]:
        return {(c["weekday"], c["hour"]) for c in out["cells"] if c["count"]}

    assert by_key(utc) == {(3, 0)}
    assert by_key(et) == {(2, 20)}
    assert by_key(tk) == {(3, 9)}


def test_invalid_timezone_falls_back_to_et() -> None:
    out = compute_heatmap([_rec("2026-05-27T14:00:00Z")], timezone="Not/A/Zone")
    assert out["timezone"] == "America/New_York"
    by_key = {(c["weekday"], c["hour"]) for c in out["cells"] if c["count"]}
    assert by_key == {(2, 10)}  # ET hour, same as default-ET test


# ── robustness ───────────────────────────────────────────────────────────


def test_malformed_and_missing_timestamps_silently_skipped() -> None:
    records: list[dict] = [
        {"published_ts": "not-an-iso-string"},
        {"published_ts": None},
        {"published_ts": ""},
        {"published_ts": "2026-05-27T14:00:00"},  # naive → skipped
        {},
        _rec("2026-05-27T14:00:00Z"),  # only valid one
    ]
    out = compute_heatmap(records)
    assert out["total_samples"] == 1
    assert out["max_count"] == 1


def test_fallback_to_created_at_when_published_ts_missing() -> None:
    records = [
        {"created_at": "2026-05-27T14:00:00Z"},  # Wed 10:00 ET
        {"published_ts": "2026-05-27T14:00:00Z"},
    ]
    out = compute_heatmap(records)
    by_key = {(c["weekday"], c["hour"]): c["count"] for c in out["cells"]}
    assert by_key[(2, 10)] == 2
