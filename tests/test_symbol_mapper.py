from __future__ import annotations

from pathlib import Path

from fusion_stack.symbol_mapper import SymbolMapper


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
