"""Tests for ``catchem.quant.co_occurrence``.

The module is pure-function and dependency-free, so these tests work on
hand-rolled dicts — no fixtures, no storage, no env wiring required.
"""

from __future__ import annotations

from typing import Any

import pytest

from catchem.quant.co_occurrence import (
    CoOccurrenceReport,
    compute_co_occurrence,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec(
    capture_id: str,
    *,
    assets: list[str] | None = None,
    reasons: list[str] | None = None,
    symbols: list[str] | None = None,
    relevance: float | None = 0.7,
) -> dict[str, Any]:
    """Build a FinancialImpactRecord-shaped dict for the views we use."""
    return {
        "capture_id": capture_id,
        "asset_classes": list(assets or []),
        "impact_reason_codes": list(reasons or []),
        "candidate_symbols": list(symbols or []),
        "finance_relevance_score": relevance,
    }


# ---------------------------------------------------------------------------
# Empty / trivial inputs
# ---------------------------------------------------------------------------


def test_empty_records_returns_zero_report() -> None:
    report = compute_co_occurrence([])
    assert isinstance(report, CoOccurrenceReport)
    assert report.total_records == 0
    assert report.distinct_assets == 0
    assert report.distinct_reasons == 0
    assert report.distinct_symbols == 0
    assert report.asset_reason_cells == ()
    assert report.strong_edges == ()
    assert report.asset_concentration == ()


def test_records_with_no_assets_or_reasons_yield_no_cells() -> None:
    records = [_rec("c1", symbols=["AAPL"]), _rec("c2", symbols=["MSFT"])]
    report = compute_co_occurrence(records)
    assert report.total_records == 2
    # No assets/reasons anywhere => no cells, no concentration entries.
    assert report.asset_reason_cells == ()
    assert report.asset_concentration == ()
    # Symbols counted but no co-mention pairs (singletons).
    assert report.distinct_symbols == 2
    assert report.strong_edges == ()


# ---------------------------------------------------------------------------
# (a) asset x reason cells — lift behaviour
# ---------------------------------------------------------------------------


def test_uniform_records_have_neutral_lift_of_one() -> None:
    # 5 records, all (equities, earnings). Only one cell exists, so lift = 1.
    records = [
        _rec(f"c{i}", assets=["equities"], reasons=["earnings"])
        for i in range(5)
    ]
    report = compute_co_occurrence(records)
    assert len(report.asset_reason_cells) == 1
    cell = report.asset_reason_cells[0]
    assert cell.asset_class == "equities"
    assert cell.reason_code == "earnings"
    assert cell.count == 5
    assert cell.lift == pytest.approx(1.0)
    assert cell.mean_relevance == pytest.approx(0.7)


def test_disjoint_asset_reason_combos_have_max_lift() -> None:
    # 10 records, each with its OWN (asset_i, reason_i). Each row and each
    # column has total 1 in a 10x10 grid => an exclusively-coupled pair has
    # lift = (count*total)/(row*col) = (1*10)/(1*1) = 10. So every diagonal
    # cell is maximally enriched (no off-diagonal noise to dilute it).
    records = [
        _rec(f"c{i}", assets=[f"asset_{i}"], reasons=[f"reason_{i}"])
        for i in range(10)
    ]
    report = compute_co_occurrence(records)
    assert len(report.asset_reason_cells) == 10
    for cell in report.asset_reason_cells:
        # All cells share the same lift here — perfect diagonal.
        assert cell.lift == pytest.approx(10.0)
        assert cell.count == 1


def test_over_represented_pair_has_lift_above_one() -> None:
    # Two asset classes, two reasons. Set up a contingency table where
    # (equities, earnings) is over-represented relative to its margins
    # but neighbours pull the diagonal back toward independence.
    #
    #               earnings   m_and_a
    #   equities      6           1        row_total = 7
    #   crypto        1           2        row_total = 3
    #   col_total     7           3        total = 10
    #
    # lift(equities, earnings) = (6 * 10) / (7 * 7) ~= 1.224  => enriched
    # lift(equities, m_and_a)  = (1 * 10) / (7 * 3) ~= 0.476  => suppressed
    # lift(crypto, earnings)   = (1 * 10) / (3 * 7) ~= 0.476  => suppressed
    # lift(crypto, m_and_a)    = (2 * 10) / (3 * 3) ~= 2.222  => most enriched
    records: list[dict[str, Any]] = []
    for i in range(6):
        records.append(
            _rec(f"a{i}", assets=["equities"], reasons=["earnings"])
        )
    records.append(_rec("b1", assets=["equities"], reasons=["m_and_a"]))
    records.append(_rec("b2", assets=["crypto"], reasons=["earnings"]))
    for i in range(2):
        records.append(_rec(f"d{i}", assets=["crypto"], reasons=["m_and_a"]))

    report = compute_co_occurrence(records)
    cells = {(c.asset_class, c.reason_code): c for c in report.asset_reason_cells}

    eq_earn = cells[("equities", "earnings")]
    assert eq_earn.count == 6
    assert eq_earn.lift > 1.0
    assert eq_earn.lift == pytest.approx(60 / 49)

    # The other diagonal is even more enriched relative to its small margins.
    cr_ma = cells[("crypto", "m_and_a")]
    assert cr_ma.lift == pytest.approx(20 / 9)
    assert cr_ma.lift > eq_earn.lift

    # Suppressed (off-diagonal) cells must drop below 1.
    assert cells[("equities", "m_and_a")].lift < 1.0
    assert cells[("crypto", "earnings")].lift < 1.0

    # Top of the sorted cells = highest lift.
    assert report.asset_reason_cells[0].asset_class == "crypto"
    assert report.asset_reason_cells[0].reason_code == "m_and_a"


def test_cells_sorted_by_lift_desc_then_count_desc() -> None:
    # Build a contingency table where two cells share the same lift but
    # differ in count, so the secondary "count DESC" tiebreaker is exercised.
    # 4 disjoint assetxreason groups, each fully exclusive:
    #   (equities, earnings)  : 3 records   => lift = 3 * 12 / (3 * 3) = 4
    #   (crypto, regulation)  : 3 records   => lift = 3 * 12 / (3 * 3) = 4
    #   (fx, central_bank)    : 4 records   => lift = 4 * 12 / (4 * 4) = 3
    #   (bonds, supply)       : 2 records   => lift = 2 * 12 / (2 * 2) = 6
    records: list[dict[str, Any]] = []
    for i in range(3):
        records.append(
            _rec(f"a{i}", assets=["equities"], reasons=["earnings"])
        )
    for i in range(3):
        records.append(
            _rec(f"b{i}", assets=["crypto"], reasons=["regulation"])
        )
    for i in range(4):
        records.append(
            _rec(f"c{i}", assets=["fx"], reasons=["central_bank"])
        )
    for i in range(2):
        records.append(
            _rec(f"d{i}", assets=["bonds"], reasons=["supply"])
        )

    report = compute_co_occurrence(records)
    lifts = [round(c.lift, 6) for c in report.asset_reason_cells]
    pairs = [(c.asset_class, c.reason_code) for c in report.asset_reason_cells]

    # Lift order: 6 > 4 > 4 > 3 (descending).
    assert lifts == [6.0, 4.0, 4.0, 3.0]
    # First slot = highest lift.
    assert pairs[0] == ("bonds", "supply")
    # The lift=4 tie is between (equities, earnings) and (crypto, regulation).
    # They tie on count too (both 3), so the final tiebreaker is alpha by
    # asset_class — "crypto" < "equities".
    assert pairs[1] == ("crypto", "regulation")
    assert pairs[2] == ("equities", "earnings")
    # Last = lift=3 cell.
    assert pairs[3] == ("fx", "central_bank")


def test_min_pair_count_filters_low_count_cells() -> None:
    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"]),
        _rec("c2", assets=["equities"], reasons=["earnings"]),
        _rec("c3", assets=["crypto"], reasons=["regulation"]),
    ]
    report = compute_co_occurrence(records, min_pair_count=2)
    assert len(report.asset_reason_cells) == 1
    assert report.asset_reason_cells[0].count == 2


def test_mean_relevance_per_cell_is_average_of_record_scores() -> None:
    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"], relevance=0.4),
        _rec("c2", assets=["equities"], reasons=["earnings"], relevance=1.0),
    ]
    report = compute_co_occurrence(records)
    cell = report.asset_reason_cells[0]
    assert cell.mean_relevance == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# (b) symbol edges
# ---------------------------------------------------------------------------


def test_three_records_with_same_pair_yield_edge_weight_three() -> None:
    records = [
        _rec("c1", symbols=["AAPL", "MSFT"]),
        _rec("c2", symbols=["AAPL", "MSFT"]),
        _rec("c3", symbols=["AAPL", "MSFT"]),
    ]
    report = compute_co_occurrence(records)
    assert len(report.strong_edges) == 1
    edge = report.strong_edges[0]
    assert edge.symbol_a == "AAPL"
    assert edge.symbol_b == "MSFT"
    assert edge.weight == 3
    # Lex-min / lex-max contract.
    assert edge.symbol_a < edge.symbol_b


def test_edge_sample_capture_ids_capped_at_three() -> None:
    records = [
        _rec(f"cap_{i}", symbols=["AAPL", "MSFT"])
        for i in range(7)
    ]
    report = compute_co_occurrence(records)
    edge = report.strong_edges[0]
    assert edge.weight == 7
    assert len(edge.sample_capture_ids) == 3
    # Records are processed in input order — first three capture_ids win.
    assert edge.sample_capture_ids == ("cap_0", "cap_1", "cap_2")


def test_min_edge_weight_excludes_singleton_pairs() -> None:
    records = [
        _rec("c1", symbols=["AAPL", "MSFT"]),  # weight 1, dropped by default
        _rec("c2", symbols=["NVDA", "AMD"]),   # weight 1, dropped by default
        _rec("c3", symbols=["NVDA", "AMD"]),   # bumps NVDA-AMD to weight 2
    ]
    report = compute_co_occurrence(records)
    assert len(report.strong_edges) == 1
    assert report.strong_edges[0].symbol_a == "AMD"
    assert report.strong_edges[0].symbol_b == "NVDA"
    assert report.strong_edges[0].weight == 2


def test_min_edge_weight_one_includes_everything() -> None:
    records = [_rec("c1", symbols=["AAPL", "MSFT"])]
    report = compute_co_occurrence(records, min_edge_weight=1)
    assert len(report.strong_edges) == 1
    assert report.strong_edges[0].weight == 1


def test_symbol_case_is_normalized() -> None:
    records = [
        _rec("c1", symbols=["aapl", "Msft"]),
        _rec("c2", symbols=["AAPL", "MSFT"]),
    ]
    report = compute_co_occurrence(records)
    assert len(report.strong_edges) == 1
    assert report.strong_edges[0].symbol_a == "AAPL"
    assert report.strong_edges[0].symbol_b == "MSFT"
    assert report.strong_edges[0].weight == 2


def test_record_with_three_symbols_emits_three_edges() -> None:
    records = [
        _rec("c1", symbols=["AAPL", "MSFT", "NVDA"]),
        _rec("c2", symbols=["AAPL", "MSFT", "NVDA"]),
    ]
    report = compute_co_occurrence(records)
    # combinations of 3 = {AAPL-MSFT, AAPL-NVDA, MSFT-NVDA}, each weight 2.
    assert len(report.strong_edges) == 3
    keys = {(e.symbol_a, e.symbol_b) for e in report.strong_edges}
    assert keys == {("AAPL", "MSFT"), ("AAPL", "NVDA"), ("MSFT", "NVDA")}
    for edge in report.strong_edges:
        assert edge.weight == 2


# ---------------------------------------------------------------------------
# (c) Herfindahl concentration
# ---------------------------------------------------------------------------


def test_single_reason_asset_has_herfindahl_one() -> None:
    records = [
        _rec(f"c{i}", assets=["equities"], reasons=["earnings"])
        for i in range(4)
    ]
    report = compute_co_occurrence(records)
    assert len(report.asset_concentration) == 1
    conc = report.asset_concentration[0]
    assert conc.asset_class == "equities"
    assert conc.record_count == 4
    assert conc.reason_count == 1
    assert conc.herfindahl_index == pytest.approx(1.0)
    assert conc.top_reasons == (("earnings", pytest.approx(1.0)),)


def test_herfindahl_in_unit_interval_for_mixed_reasons() -> None:
    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"]),
        _rec("c2", assets=["equities"], reasons=["m_and_a"]),
        _rec("c3", assets=["equities"], reasons=["regulation"]),
        _rec("c4", assets=["equities"], reasons=["earnings"]),
    ]
    report = compute_co_occurrence(records)
    conc = report.asset_concentration[0]
    assert conc.asset_class == "equities"
    assert conc.record_count == 4
    assert conc.reason_count == 3
    # earnings: 2/4 = 0.5, m_and_a: 1/4 = 0.25, regulation: 1/4 = 0.25
    # HHI = 0.25 + 0.0625 + 0.0625 = 0.375
    assert conc.herfindahl_index == pytest.approx(0.375)
    assert 0.0 <= conc.herfindahl_index <= 1.0
    # Top reasons sorted by share DESC.
    assert conc.top_reasons[0] == ("earnings", pytest.approx(0.5))


def test_top_reasons_capped_at_five() -> None:
    # One asset with 7 distinct reasons, equal weight => ties broken alpha.
    reasons = [f"r{i}" for i in range(7)]
    records = [
        _rec(f"c{i}", assets=["equities"], reasons=[reasons[i]])
        for i in range(7)
    ]
    report = compute_co_occurrence(records)
    conc = report.asset_concentration[0]
    assert conc.reason_count == 7
    assert len(conc.top_reasons) == 5
    # All shares equal at 1/7; alpha-tiebreak picks r0..r4.
    picked = [r for r, _ in conc.top_reasons]
    assert picked == ["r0", "r1", "r2", "r3", "r4"]


def test_asset_concentration_sorted_by_record_count_desc() -> None:
    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"]),
        _rec("c2", assets=["equities"], reasons=["earnings"]),
        _rec("c3", assets=["equities"], reasons=["earnings"]),
        _rec("c4", assets=["crypto"], reasons=["regulation"]),
        _rec("c5", assets=["fx"], reasons=["central_bank"]),
        _rec("c6", assets=["fx"], reasons=["central_bank"]),
    ]
    report = compute_co_occurrence(records)
    counts = [c.record_count for c in report.asset_concentration]
    assert counts == sorted(counts, reverse=True)
    # Specifically: equities(3), fx(2), crypto(1).
    classes = [c.asset_class for c in report.asset_concentration]
    assert classes == ["equities", "fx", "crypto"]


def test_asset_without_any_reason_gets_neutral_concentration() -> None:
    records = [_rec("c1", assets=["equities"], reasons=[])]
    report = compute_co_occurrence(records)
    assert len(report.asset_concentration) == 1
    conc = report.asset_concentration[0]
    assert conc.asset_class == "equities"
    assert conc.record_count == 1
    assert conc.reason_count == 0
    assert conc.herfindahl_index == 0.0
    assert conc.top_reasons == ()


# ---------------------------------------------------------------------------
# Determinism & summary fields
# ---------------------------------------------------------------------------


def test_summary_distinct_counters_are_correct() -> None:
    records = [
        _rec("c1", assets=["equities", "crypto"], reasons=["earnings"],
             symbols=["AAPL", "MSFT"]),
        _rec("c2", assets=["equities"], reasons=["m_and_a", "earnings"],
             symbols=["aapl", "GOOG"]),  # lowercase aapl should fold into AAPL
    ]
    report = compute_co_occurrence(records)
    assert report.total_records == 2
    assert report.distinct_assets == 2     # equities, crypto
    assert report.distinct_reasons == 2    # earnings, m_and_a
    assert report.distinct_symbols == 3    # AAPL, MSFT, GOOG (case folded)


def test_report_is_deterministic_for_same_input() -> None:
    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"],
             symbols=["AAPL", "MSFT"]),
        _rec("c2", assets=["equities"], reasons=["m_and_a"],
             symbols=["AAPL", "MSFT"]),
        _rec("c3", assets=["crypto"], reasons=["regulation"],
             symbols=["BTC", "ETH"]),
        _rec("c4", assets=["crypto"], reasons=["regulation"],
             symbols=["BTC", "ETH"]),
    ]
    a = compute_co_occurrence(records)
    b = compute_co_occurrence(records)
    assert a == b
    # Pin actual sort order to lock the contract.
    edges = a.strong_edges
    assert [e.symbol_a for e in edges] == ["AAPL", "BTC"]
    assert [e.symbol_b for e in edges] == ["MSFT", "ETH"]


def test_top_n_caps_apply() -> None:
    # 12 disjoint (asset, reason) records => 12 cells of lift=1 each.
    records = [
        _rec(f"c{i}", assets=[f"a{i}"], reasons=[f"r{i}"])
        for i in range(12)
    ]
    # And 6 distinct symbol-pair edges, each repeated twice for weight 2.
    pairs = [
        ("AA", "BB"), ("CC", "DD"), ("EE", "FF"),
        ("GG", "HH"), ("II", "JJ"), ("KK", "LL"),
    ]
    for idx, (s1, s2) in enumerate(pairs):
        records.append(_rec(f"e{idx}a", symbols=[s1, s2]))
        records.append(_rec(f"e{idx}b", symbols=[s1, s2]))

    report = compute_co_occurrence(records, top_n_cells=5, top_n_edges=3)
    assert len(report.asset_reason_cells) == 5
    assert len(report.strong_edges) == 3


def test_strong_edges_sorted_by_weight_desc_then_alpha() -> None:
    records = [
        _rec("c1", symbols=["NVDA", "AMD"]),  # weight 1 (filtered by default)
        _rec("c2", symbols=["AAPL", "MSFT"]),
        _rec("c3", symbols=["AAPL", "MSFT"]),  # weight 2
        _rec("c4", symbols=["GOOG", "META"]),
        _rec("c5", symbols=["GOOG", "META"]),
        _rec("c6", symbols=["GOOG", "META"]),  # weight 3 — top
    ]
    report = compute_co_occurrence(records)
    assert [e.weight for e in report.strong_edges] == [3, 2]
    assert report.strong_edges[0].symbol_a == "GOOG"
    assert report.strong_edges[0].symbol_b == "META"
    assert report.strong_edges[1].symbol_a == "AAPL"
    assert report.strong_edges[1].symbol_b == "MSFT"


# ---------------------------------------------------------------------------
# Input-coercion helpers (_as_iter / _clean_set / _relevance / _capture_id)
# ---------------------------------------------------------------------------


def test_as_iter_none_becomes_empty() -> None:
    from catchem.quant.co_occurrence import _as_iter  # type: ignore[attr-defined]

    assert tuple(_as_iter(None)) == ()


def test_as_iter_scalar_is_wrapped() -> None:
    """A non-collection scalar is wrapped in a 1-tuple."""

    from catchem.quant.co_occurrence import _as_iter  # type: ignore[attr-defined]

    assert tuple(_as_iter("EQUITY")) == ("EQUITY",)
    assert tuple(_as_iter(42)) == (42,)


def test_as_iter_passes_collections_through() -> None:
    from catchem.quant.co_occurrence import _as_iter  # type: ignore[attr-defined]

    assert tuple(_as_iter(["a", "b"])) == ("a", "b")
    assert tuple(_as_iter(("a",))) == ("a",)


def test_clean_set_skips_none_and_blanks() -> None:
    from catchem.quant.co_occurrence import _clean_set  # type: ignore[attr-defined]

    assert _clean_set(["EQUITY", None, "  ", " FX "]) == frozenset(
        {"EQUITY", "FX"}
    )


def test_relevance_none_when_missing_or_unparseable() -> None:
    from catchem.quant.co_occurrence import _relevance  # type: ignore[attr-defined]

    assert _relevance({}) is None
    assert _relevance({"finance_relevance_score": None}) is None
    assert _relevance({"finance_relevance_score": "not-a-number"}) is None
    assert _relevance({"finance_relevance_score": [1, 2]}) is None


def test_relevance_parses_numeric_strings_and_floats() -> None:
    from catchem.quant.co_occurrence import _relevance  # type: ignore[attr-defined]

    assert _relevance({"finance_relevance_score": "0.5"}) == pytest.approx(0.5)
    assert _relevance({"finance_relevance_score": 1}) == pytest.approx(1.0)


def test_capture_id_stringifies_and_defaults() -> None:
    from catchem.quant.co_occurrence import _capture_id  # type: ignore[attr-defined]

    assert _capture_id({"capture_id": 123}) == "123"
    assert _capture_id({"capture_id": None}) == ""
    assert _capture_id({}) == ""


# ---------------------------------------------------------------------------
# (a) cells — missing-relevance + neutral-lift fallbacks
# ---------------------------------------------------------------------------


def test_cell_mean_relevance_zero_when_all_scores_missing() -> None:
    """A cell whose every record lacks a relevance score reports 0.0."""

    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"], relevance=None),
        _rec("c2", assets=["equities"], reasons=["earnings"], relevance=None),
    ]
    report = compute_co_occurrence(records)
    assert len(report.asset_reason_cells) == 1
    cell = report.asset_reason_cells[0]
    assert cell.count == 2
    assert cell.mean_relevance == 0.0


def test_cell_mean_relevance_averages_only_present_scores() -> None:
    """Records missing a score don't dilute the mean toward zero."""

    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"], relevance=0.6),
        _rec("c2", assets=["equities"], reasons=["earnings"], relevance=None),
        _rec("c3", assets=["equities"], reasons=["earnings"], relevance=0.8),
    ]
    report = compute_co_occurrence(records)
    cell = report.asset_reason_cells[0]
    assert cell.count == 3
    # Only the two present scores are averaged: (0.6 + 0.8) / 2 = 0.7.
    assert cell.mean_relevance == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# (b) edges — capture-id dedup + missing-id handling
# ---------------------------------------------------------------------------


def test_edge_samples_dedupe_within_one_edge() -> None:
    """The same capture_id seen twice on an edge is recorded only once."""

    records = [
        _rec("dup", symbols=["AAPL", "MSFT"]),
        _rec("dup", symbols=["AAPL", "MSFT"]),  # identical id — must not repeat
        _rec("uniq", symbols=["AAPL", "MSFT"]),
    ]
    report = compute_co_occurrence(records)
    edge = report.strong_edges[0]
    assert edge.weight == 3
    assert edge.sample_capture_ids == ("dup", "uniq")


def test_edge_with_blank_capture_ids_has_no_samples() -> None:
    """Edges built from records lacking capture_ids carry empty samples."""

    records = [
        _rec("", symbols=["AAPL", "MSFT"]),
        _rec("", symbols=["AAPL", "MSFT"]),
    ]
    report = compute_co_occurrence(records)
    edge = report.strong_edges[0]
    assert edge.weight == 2
    assert edge.sample_capture_ids == ()


# ---------------------------------------------------------------------------
# (c) Herfindahl clamp + asset-without-reason in a multi-asset stream
# ---------------------------------------------------------------------------


def test_herfindahl_clamped_to_one_for_single_reason() -> None:
    """A lone reason yields exactly 1.0 — never above (defensive clamp)."""

    records = [_rec("c1", assets=["equities"], reasons=["earnings"])]
    report = compute_co_occurrence(records)
    conc = report.asset_concentration[0]
    assert conc.herfindahl_index == 1.0
    assert conc.herfindahl_index <= 1.0


def test_asset_with_no_reasons_among_reasoned_assets() -> None:
    """An asset that never co-occurs with a reason gets neutral defaults
    even when other assets in the same stream are fully reasoned."""

    records = [
        _rec("c1", assets=["equities"], reasons=["earnings"]),
        _rec("c2", assets=["equities"], reasons=["earnings"]),
        _rec("c3", assets=["commodities"], reasons=[]),  # no reasons at all
    ]
    report = compute_co_occurrence(records)
    by_asset = {c.asset_class: c for c in report.asset_concentration}
    assert by_asset["commodities"].reason_count == 0
    assert by_asset["commodities"].herfindahl_index == 0.0
    assert by_asset["commodities"].top_reasons == ()
    # The reasoned asset is unaffected.
    assert by_asset["equities"].herfindahl_index == pytest.approx(1.0)
