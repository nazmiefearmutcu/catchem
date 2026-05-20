from __future__ import annotations

from pathlib import Path

from catchem.symbol_mapper import SymbolMapper


def test_internal_registry_resolves_majors() -> None:
    m = SymbolMapper()
    matches = m.map_text("Apple unveils new chip; Microsoft Azure outage")
    syms = {x.symbol for x in matches}
    assert "AAPL" in syms
    assert "MSFT" in syms


def test_cashtag_short_circuits() -> None:
    m = SymbolMapper()
    matches = m.map_text("$NVDA $AMD both up")
    syms = {x.symbol for x in matches}
    assert "NVDA" in syms
    assert "AMD" in syms


def test_crypto_ticker_aliases_resolve_to_yahoo_symbols() -> None:
    m = SymbolMapper()
    matches = m.map_text("XRP gives back gains after Bitcoin and ETH rally")
    syms = {x.symbol for x in matches}
    assert "XRP-USD" in syms
    assert "BTC-USD" in syms
    assert "ETH-USD" in syms


def test_parenthetical_ticker_is_symbol_but_macro_acronym_is_not() -> None:
    m = SymbolMapper()
    matches = m.map_text("Bitcoin Depot (BTM) files for Chapter 11 after FOMC minutes")
    syms = {x.symbol for x in matches}
    assert "BTM" in syms
    assert "FOMC" not in syms


def test_missing_newsimpact_root_degrades_gracefully(tmp_path: Path) -> None:
    m = SymbolMapper(newsimpact_root=tmp_path / "does-not-exist")
    # internal registry should still be populated
    assert len(m.alias_dict()) > 50


def test_fuzzy_only_used_for_short_text() -> None:
    m = SymbolMapper()
    # Long text bypasses fuzzy fallback to keep things fast.
    matches = m.map_text("x" * 500 + " Apple ")
    syms = {x.symbol for x in matches}
    # exact alias match still works
    assert "AAPL" in syms


def test_aliases_do_not_match_inside_words() -> None:
    m = SymbolMapper()
    matches = m.map_text(
        "5 money moves that made this dad a millionaire\n"
        "A low starting salary and an unaffordable housing market did not stop him."
    )
    syms = {x.symbol for x in matches}
    assert "F" not in syms
    assert "GC=F" not in syms
