from __future__ import annotations

import json
from pathlib import Path

from catchem.chart_context import ChartContextReader


def test_missing_root_returns_unavailable(tmp_path: Path) -> None:
    r = ChartContextReader(newsimpact_root=tmp_path / "nope")
    ctx = r.lookup("AAPL")
    assert ctx.available is False
    assert ctx.note == "no_chart_artifact_for_symbol"


def test_synthetic_chart_artifact_is_read(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "AAPL_chart.json").write_text(
        json.dumps({
            "close": [100.0 + i for i in range(30)],
        }),
        encoding="utf-8",
    )
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")  # filename-derived symbol uppercased
    assert ctx.available is True
    assert ctx.last_price is not None
    assert ctx.last_return_1d is not None


def test_last_return_5d_uses_correct_divisor_guard(tmp_path: Path) -> None:
    """BUG-X regression: pre-fix `if closes[0]:` guarded the
    `last_return_5d` computation, but the actual divisor was `closes[-6]`.
    A series with `closes[0]==0` (pathological but possible — e.g. a
    delisting marker stub at the head of the window) would SKIP the
    computation even though closes[-6] is valid. The proper guard checks
    the divisor.

    Synthetic data: pin closes[0]=0 and ensure closes[-6]!=0. After the fix
    `last_return_5d` is computed from the valid divisor.
    """
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    # 21 closes — first is the pathological 0, the rest are >0.
    closes = [0.0] + [100.0 + i for i in range(20)]
    (chunks / "AAPL_chart.json").write_text(
        json.dumps({"close": closes}),
        encoding="utf-8",
    )
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")
    assert ctx.available is True
    assert ctx.last_return_5d is not None, (
        "Pre-fix `if closes[0]` short-circuited even though the actual "
        "divisor (closes[-6]) is non-zero. Fix should guard on the divisor."
    )


def test_chart_context_is_labeled_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "BTC_price.json").write_text(json.dumps({"last_price": 78000}), encoding="utf-8")
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("BTC_PRICE")
    meta = ctx.to_metadata()
    assert meta["note"] == "context_only_not_causal"


def test_empty_symbol_lookup(tmp_path: Path) -> None:
    r = ChartContextReader(newsimpact_root=tmp_path)
    ctx = r.lookup("")
    assert ctx.available is False
    assert ctx.note == "empty_symbol"


def test_governance_file_skipped(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    gov = root / "chunks" / "governance"
    gov.mkdir(parents=True)
    (gov / "AAPL_chart.json").write_text(json.dumps({"last_price": 150}), encoding="utf-8")
    
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")
    assert ctx.available is False
    assert ctx.note == "no_chart_artifact_for_symbol"


def test_unmatching_filename_skipped(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "AAPL_info.json").write_text(json.dumps({"last_price": 150}), encoding="utf-8")
    
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_info")
    assert ctx.available is False


def test_exception_in_build_index_loop(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "AAPL_chart.json").write_text(json.dumps({"last_price": 150}), encoding="utf-8")
    
    # Mock p.name to raise an exception
    
    def fake_name_prop(self):
        if "AAPL" in str(self):
            raise ValueError("Mock name exception")
        return "AAPL_chart.json"
        
    monkeypatch.setattr(Path, "name", property(fake_name_prop))
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")
    assert ctx.available is False


def test_invalid_json_parse_failure(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "AAPL_chart.json").write_text("{invalid_json", encoding="utf-8")
    
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")
    assert ctx.available is False
    assert ctx.note == "parse_failed"


def test_extract_features_non_dict(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "AAPL_chart.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")
    assert ctx.available is True
    assert ctx.last_price is None


def test_extract_features_all_aliases(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    
    # ret_1d / ret_5d / rv_20d / close
    (chunks / "AAPL_chart.json").write_text(
        json.dumps({
            "ret_1d": 0.015,
            "ret_5d": 0.05,
            "rv_20d": 0.22,
            "close": 155.0
        }),
        encoding="utf-8"
    )
    
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("AAPL_chart")
    assert ctx.available is True
    assert ctx.last_return_1d == 0.015
    assert ctx.last_return_5d == 0.05
    assert ctx.realized_vol_20d == 0.22
    assert ctx.last_price == 155.0

    # r1 / r5 / vol20 / px
    (chunks / "MSFT_chart.json").write_text(
        json.dumps({
            "r1": 0.02,
            "r5": 0.06,
            "vol20": 0.25,
            "px": 300.0
        }),
        encoding="utf-8"
    )
    r2 = ChartContextReader(newsimpact_root=root)
    ctx2 = r2.lookup("MSFT_chart")
    assert ctx2.available is True
    assert ctx2.last_return_1d == 0.02
    assert ctx2.last_return_5d == 0.06
    assert ctx2.realized_vol_20d == 0.25
    assert ctx2.last_price == 300.0


def test_series_closes_zero_division_and_vol_math(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    
    # 1. closes[-2] is zero
    closes_zero_1d = [100.0] * 19 + [0.0, 105.0]
    (chunks / "ZERO1D_chart.json").write_text(
        json.dumps({"prices": closes_zero_1d}),
        encoding="utf-8"
    )
    
    # 2. closes[-6] is zero
    closes_zero_5d = [100.0] * 15 + [0.0] + [100.0] * 5
    (chunks / "ZERO5D_chart.json").write_text(
        json.dumps({"prices": closes_zero_5d}),
        encoding="utf-8"
    )

    r = ChartContextReader(newsimpact_root=root)
    ctx_1d = r.lookup("ZERO1D_chart")
    assert ctx_1d.available is True
    assert ctx_1d.last_return_1d is None
    
    ctx_5d = r.lookup("ZERO5D_chart")
    assert ctx_5d.available is True
    assert ctx_5d.last_return_5d is None


def test_series_vol_exceptions_and_empty_returns(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    
    # 1. Closes series with string elements (raises ValueError during float coercion)
    (chunks / "ERR_chart.json").write_text(
        json.dumps({"close": ["100.0"] * 20 + ["bad_value"]}),
        encoding="utf-8"
    )
    
    # 2. Closes series with all zeros (log-return lr list becomes empty because closes[i-1] is 0)
    (chunks / "ZEROALL_chart.json").write_text(
        json.dumps({"close": [0.0] * 30}),
        encoding="utf-8"
    )

    r = ChartContextReader(newsimpact_root=root)
    ctx_err = r.lookup("ERR_chart")
    assert ctx_err.available is True
    assert ctx_err.last_price is None # Exception caught and logged
    
    ctx_zero = r.lookup("ZEROALL_chart")
    assert ctx_zero.available is True
    assert ctx_zero.realized_vol_20d is None

