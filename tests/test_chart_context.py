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


def test_chart_context_is_labeled_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / "ni"
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "BTC_price.json").write_text(json.dumps({"last_price": 78000}), encoding="utf-8")
    r = ChartContextReader(newsimpact_root=root)
    ctx = r.lookup("BTC_PRICE")
    meta = ctx.to_metadata()
    assert meta["note"] == "context_only_not_causal"
