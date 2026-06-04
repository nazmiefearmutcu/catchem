from __future__ import annotations

from catchem.entity_linker import EntityLinker
from catchem.evidence import clean_boilerplate_text


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


# ── BUG-V regression: word-boundary, not substring, for central banks/indices ──
#
# Pre-fix: `if cb in joined` was a plain Python substring check, so "Fed"
# (a known central-bank token) matched "Federation", "Federated", "Federalist".
# Symmetric bug on indices: "Dow" matched "Down", "Dowager". Other entity
# kinds (commodity/crypto) already used \b...\b — the inconsistency was the
# real tell.


def test_fed_central_bank_does_not_match_federation() -> None:
    e = EntityLinker()
    h = e.extract(
        title="UN Federation of Trade Unions condemns sanctions", text="The federation released a statement."
    )
    cbs = h.by_kind("central_bank")
    assert "Fed" not in cbs, (
        "Substring 'Fed' inside 'Federation' must NOT trigger central-bank match. "
        f"Got cbs={cbs}. Use word-boundary regex (\\bFed\\b) like commodities/crypto."
    )


def test_fed_central_bank_does_match_real_fed_mention() -> None:
    e = EntityLinker()
    h = e.extract(title="Fed hikes rates again", text="The Fed surprised markets.")
    assert "Fed" in h.by_kind("central_bank")


def test_dow_index_does_not_match_inside_down() -> None:
    e = EntityLinker()
    h = e.extract(title="Markets are down on rate fears", text="The shutdown weighs on sentiment.")
    idxs = h.by_kind("index")
    assert "Dow" not in idxs, (
        f"Substring 'Dow' inside 'down'/'shutdown' must NOT trigger index match. Got idxs={idxs}."
    )


def test_dow_index_does_match_real_dow_mention() -> None:
    e = EntityLinker()
    h = e.extract(title="Dow Jones closes at record", text="The Dow gained 1%.")
    idxs = h.by_kind("index")
    assert any("Dow" in i for i in idxs), idxs


def test_sp_index_with_special_chars_still_word_bounded() -> None:
    """`S&P 500` has `&` (non-alnum) — word-boundary regex must still match it.
    `re.escape` handles the `&`, and `\\b` anchors on the surrounding text."""
    e = EntityLinker()
    h = e.extract(title="S&P 500 hits new high", text="S&P 500 rose 2%.")
    idxs = h.by_kind("index")
    assert any("S&P" in i for i in idxs)


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


def test_entity_linker_coverage_gaps() -> None:
    # 1. Short and blank company aliases to cover _contains_alias early exits
    e = EntityLinker(company_aliases={"A": "AAPL", " ": "GOOG", "": "MSFT"})
    h = e.extract(title="A company", text="Another company")
    # None of these short/blank aliases should map to tickers or companies
    assert "AAPL" not in h.tickers
    assert "A" not in h.by_kind("company")
    assert "GOOG" not in h.tickers
    assert "MSFT" not in h.tickers

    # 2. Cashtags, tickers and unique_texts properties/methods on EntityHits
    # We want to make sure the helper methods/properties are fully covered
    e2 = EntityLinker(company_aliases={"Apple": "AAPL"})
    h2 = e2.extract(title="$AAPL beats", text="Apple held an event and $MSFT rose. Also $AAPL rose.")
    assert h2.cashtags == ["AAPL", "MSFT"]
    assert h2.tickers == ["AAPL"]
    assert h2.unique_texts() == ["AAPL", "MSFT", "Apple"]

    # 3. Ticker denylist hit in paren regex
    e3 = EntityLinker()
    h3 = e3.extract(title="The CEO (CEO) resigned", text="The SEC (SEC) investigated Microsoft (MSFT).")
    # (CEO) and (SEC) should be ignored, but (MSFT) should be detected
    assert "CEO" not in h3.tickers
    assert "SEC" not in h3.tickers
    assert "MSFT" in h3.tickers

    # 4. Known currencies detection
    h4 = e3.extract(title="USD drops", text="EUR rises.")
    assert "USD" in h4.by_kind("currency")
    assert "EUR" in h4.by_kind("currency")

    # 5. Known commodities detection
    h5 = e3.extract(title="crude oil prices", text="gold rises.")
    assert "crude" in h5.by_kind("commodity")
    assert "gold" in h5.by_kind("commodity")

    # 6. Known crypto detection
    h6 = e3.extract(title="Bitcoin is up", text="BTC is also up.")
    assert "Bitcoin" in h6.by_kind("crypto")
    assert "BTC" in h6.by_kind("crypto")


def test_alias_pattern_cache() -> None:
    from catchem.entity_linker import _compile_alias_pattern

    _compile_alias_pattern.cache_clear()
    pat1 = _compile_alias_pattern("testalias")
    pat2 = _compile_alias_pattern("testalias")
    assert pat1 is pat2
    stats = _compile_alias_pattern.cache_info()
    assert stats.hits == 1
    assert stats.misses == 1
