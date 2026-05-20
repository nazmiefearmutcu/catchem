from __future__ import annotations

from fusion_stack.entity_linker import EntityLinker
from fusion_stack.evidence import clean_boilerplate_text


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


def test_company_alias_does_not_match_inside_words() -> None:
    e = EntityLinker(company_aliases={"Ford": "F", "gold": "GC=F"})
    h = e.extract(
        title="5 money moves that made this dad a millionaire",
        text="A low starting salary and an unaffordable housing market did not stop him.",
    )
    assert "Ford" not in h.by_kind("company")
    assert "F" not in h.tickers
    assert "gold" not in h.by_kind("company")


def test_boilerplate_cleaner_removes_social_cta_before_entity_linking() -> None:
    text = "The health agency reported an outbreak. Follow us on Facebook and Twitter for updates."
    cleaned = clean_boilerplate_text(text)
    e = EntityLinker(company_aliases={"Facebook": "META"})
    h = e.extract(title="Health agency reports outbreak", text=cleaned)
    assert "Facebook" not in h.by_kind("company")
    assert "META" not in h.tickers


def test_empty_text_returns_no_hits() -> None:
    e = EntityLinker()
    h = e.extract(title="", text="")
    assert h.hits == []
