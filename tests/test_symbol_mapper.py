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


def test_exchange_prefixed_paren_ticker_extracts_subject() -> None:
    """Wire-service body copy from Benzinga / CNBC / MarketWatch / Reuters /
    Bloomberg uses `(NASDAQ: AAPL)`, `(NYSE: BRK.B)`, `(NASDAQ:LI)`. The
    bare-paren regex used to miss these entirely — articles ABOUT Li Auto
    would surface candidate_symbols = ["TSLA", "MS", ...] (background
    mentions only) and entirely omit the subject "LI". Pin the fix here.
    """
    m = SymbolMapper()
    text = (
        "Shares of Tesla Inc.'s (NASDAQ: TSLA) Chinese rival Li Auto Inc. "
        "(NASDAQ: LI) have seen its value score surge. Morgan Stanley (NYSE: MS) "
        "raised its target."
    )
    matches = m.map_text(text)
    syms = {x.symbol for x in matches}
    # The KEY regression assertion: LI shows up (was missed before).
    assert "LI" in syms, f"LI missing from exchange-prefixed parens; got {syms}"
    # All three subjects should be captured.
    assert "TSLA" in syms
    assert "MS" in syms


def test_paren_ticker_handles_compact_and_dotted_forms() -> None:
    """Cover `(NASDAQ:LI)` (no space) and `(NYSE: BRK.B)` (dotted ticker)."""
    m = SymbolMapper()
    for variant, expected in [
        ("Li Auto (NASDAQ:LI) opened up", "LI"),
        ("Berkshire (NYSE: BRK.B) trimmed Apple", "BRK.B"),
        ("Plain (AAPL) still works", "AAPL"),
        ("Lower-tier (LSE:HSBA) listed", "HSBA"),
    ]:
        matches = m.map_text(variant)
        syms = {x.symbol for x in matches}
        assert expected in syms, f"{expected!r} missing from {variant!r}; got {syms}"


def test_paren_ticker_still_denies_macro_acronyms_in_prefixed_form() -> None:
    """`(NASDAQ: SEC)` is nonsense but the regex would happily capture SEC.
    The denylist must still kick in regardless of which form matched.
    """
    m = SymbolMapper()
    matches = m.map_text("Filing details (NASDAQ: SEC) — placeholder")
    syms = {x.symbol for x in matches}
    assert "SEC" not in syms, f"SEC leaked through denylist; got {syms}"


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


def test_merge_yaml_valid_and_invalid(tmp_path: Path) -> None:
    # 1. Valid YAML with aliases
    yaml_file = tmp_path / "extra_aliases.yaml"
    yaml_file.write_text("aliases:\n  MyCustomAlias: MY_SYM\n", encoding="utf-8")
    m = SymbolMapper(config_path=yaml_file)
    assert m.alias_dict().get("MyCustomAlias") == "MY_SYM"

    # 2. Invalid YAML (triggers warning block)
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("aliases:\n  [unbalanced bracket", encoding="utf-8")
    m2 = SymbolMapper(config_path=bad_yaml)
    assert "MyCustomAlias" not in m2.alias_dict()


def test_maybe_merge_newsimpact(tmp_path: Path) -> None:
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True)

    # Write a valid JSON manifest
    valid_json = manifests_dir / "valid.json"
    import json as _json

    valid_json.write_text(_json.dumps({"aliases": {"NewsimpactCompany": "NIMP"}}), encoding="utf-8")

    # Write a corrupt JSON manifest
    corrupt_json = manifests_dir / "corrupt.json"
    corrupt_json.write_text("invalid json {", encoding="utf-8")

    # Write a non-dict JSON manifest
    nondict_json = manifests_dir / "nondict.json"
    nondict_json.write_text("123", encoding="utf-8")

    # Write a manifest using symbol_aliases key
    alt_json = manifests_dir / "alt.json"
    alt_json.write_text(_json.dumps({"symbol_aliases": {"AltCompany": "ALT"}}), encoding="utf-8")

    # Let's merge them
    m = SymbolMapper(newsimpact_root=tmp_path)
    aliases = m.alias_dict()
    assert aliases.get("NewsimpactCompany") == "NIMP"
    assert aliases.get("AltCompany") == "ALT"

    # Test manifest limit (12 scanned max)
    # Create 15 manifest files
    limit_root = tmp_path / "limit_test"
    limit_manifests = limit_root / "manifests"
    limit_manifests.mkdir(parents=True)
    for i in range(15):
        (limit_manifests / f"m_{i}.json").write_text(
            _json.dumps({"aliases": {f"Company_{i}": f"SYM_{i}"}}), encoding="utf-8"
        )
    m_limit = SymbolMapper(newsimpact_root=limit_root)
    # We should have successfully scanned some but exactly 12 (or stopped at 12)
    scanned_count = sum(1 for i in range(15) if f"Company_{i}" in m_limit.alias_dict())
    assert scanned_count <= 12


def test_symbol_mapper_missing_branches(tmp_path: Path) -> None:
    m = SymbolMapper()

    # 1. not text
    assert m.map_text("") == []
    assert m.map_text(None) == []

    # 2. _TICKER_DENYLIST in cashtag
    matches_deny = m.map_text("Check out this $IPO")
    assert not any(x.symbol == "IPO" for x in matches_deny)

    # 3. sym in seen for duplicate cashtags
    matches_dup = m.map_text("$AAPL and $AAPL")
    aapl_matches = [x for x in matches_dup if x.symbol == "AAPL"]
    assert len(aapl_matches) == 1

    # 4. sym in seen for duplicate paren/cashtag
    matches_dup_paren = m.map_text("$AAPL and (AAPL)")
    aapl_matches_p = [x for x in matches_dup_paren if x.symbol == "AAPL"]
    assert len(aapl_matches_p) == 1

    # 5. Fuzzy match continue: score >= 100.0 and no word-boundary match
    yaml_file = tmp_path / "fuzzy_test.yaml"
    yaml_file.write_text("aliases:\n  Brent: BRNT\n", encoding="utf-8")
    m_fuzzy = SymbolMapper(config_path=yaml_file)
    matches_fuzzy = m_fuzzy.map_text("We visited Brentwood today")
    assert not any(x.symbol == "BRNT" for x in matches_fuzzy)

    # 5b. Fuzzy match accepted: score < 100.0 (e.g. Microsft vs Microsoft)
    matches_fuzzy_ok = m.map_text("We love Microsft")
    assert any(x.symbol == "MSFT" for x in matches_fuzzy_ok)

    # 6. extra is not a dict in _merge_yaml
    yaml_file_nondict = tmp_path / "nondict_aliases.yaml"
    yaml_file_nondict.write_text("aliases: not-a-dict\n", encoding="utf-8")
    m_nondict = SymbolMapper(config_path=yaml_file_nondict)
    assert len(m_nondict.map_text("not-a-dict")) == 0

    # 7. aliases is not a dict in _maybe_merge_newsimpact
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    nondict_json = manifests_dir / "nondict_aliases.json"
    import json as _json

    nondict_json.write_text(_json.dumps({"aliases": "not-a-dict"}), encoding="utf-8")
    m_json_nondict = SymbolMapper(newsimpact_root=tmp_path)
    assert len(m_json_nondict.map_text("not-a-dict")) == 0


def test_alias_pattern_cache() -> None:
    from catchem.symbol_mapper import _alias_pattern

    _alias_pattern.cache_clear()
    pat1 = _alias_pattern("testalias")
    pat2 = _alias_pattern("testalias")
    assert pat1 is pat2
    stats = _alias_pattern.cache_info()
    assert stats.hits == 1
    assert stats.misses == 1
