from __future__ import annotations

from fusion_stack.entity_linker import EntityLinker


def test_cashtag_detected() -> None:
    e = EntityLinker()
    h = e.extract(title="$AAPL beats", text="$AAPL beat earnings. $MSFT also rose.")
    assert "AAPL" in h.cashtags
    assert "MSFT" in h.cashtags


def test_central_bank_and_index_detected() -> None:
    e = EntityLinker()
    h = e.extract(
        title="Federal Reserve hikes, S&P 500 falls",
        text="The Fed raised rates today. The S&P 500 closed lower.",
    )
    cbs = h.by_kind("central_bank")
    idxs = h.by_kind("index")
    assert any(c in cbs for c in ("Federal Reserve", "Fed"))
    assert any("S&P 500" in s or s == "S&P" for s in idxs)


def test_company_alias_maps_to_ticker() -> None:
    e = EntityLinker(company_aliases={"Apple": "AAPL"})
    h = e.extract(title="Apple unveils new chip", text="Apple held an event.")
    assert "AAPL" in h.tickers
    assert "Apple" in h.by_kind("company")


def test_empty_text_returns_no_hits() -> None:
    e = EntityLinker()
    h = e.extract(title="", text="")
    assert h.hits == []
