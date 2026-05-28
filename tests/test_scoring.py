"""Dedicated unit tests for `catchem.scoring`.

Each test pins one documented contract from the module's `score()` docstring
and the `estimate_entity_density()` helper so a regression points at exactly
one expectation. Deterministic, no network, no ML.

The default taxonomy thresholds these tests rely on (configs/taxonomy.yaml):
    finance_relevance_floor = 0.35
    negative_class_block     = 0.65
    asset_class_min          = 0.25
    reason_code_min          = 0.25
"""

from __future__ import annotations

import pytest

from catchem.scoring import (
    ScoringInputs,
    ScoringOutputs,
    estimate_entity_density,
    score,
)
from catchem.taxonomy import Taxonomy, default_taxonomy_path, load_taxonomy


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def taxonomy() -> Taxonomy:
    return load_taxonomy(default_taxonomy_path())


def _inputs(
    *,
    prefilter_rule_score: float = 0.0,
    domain_prior: float = 0.0,
    source_type_prior: float = 0.0,
    asset_class_scores: dict[str, float] | None = None,
    reason_code_scores: dict[str, float] | None = None,
    negative_class_scores: dict[str, float] | None = None,
    sentiment_confidence: float = 0.0,
    entity_density: float = 0.0,
) -> ScoringInputs:
    """Build ScoringInputs with all-zero defaults; override per test."""
    return ScoringInputs(
        prefilter_rule_score=prefilter_rule_score,
        domain_prior=domain_prior,
        source_type_prior=source_type_prior,
        asset_class_scores=asset_class_scores or {},
        reason_code_scores=reason_code_scores or {},
        negative_class_scores=negative_class_scores or {},
        sentiment_confidence=sentiment_confidence,
        entity_density=entity_density,
    )


# ---------------------------------------------------------------------------
# Output shape / determinism
# ---------------------------------------------------------------------------


def test_score_returns_scoring_outputs_with_full_component_map(taxonomy) -> None:
    """Every documented component key is present in component_scores."""
    out = score(_inputs(asset_class_scores={"equities": 0.5}), taxonomy)
    assert isinstance(out, ScoringOutputs)
    expected_keys = {
        "asset_class_max",
        "reason_code_max",
        "negative_class_max",
        "prefilter_rule_score",
        "domain_prior",
        "source_type_prior",
        "sentiment_confidence",
        "entity_density",
        "raw_relevance_score",
        "threshold_floor",
    }
    assert set(out.component_scores) == expected_keys


def test_score_is_deterministic(taxonomy) -> None:
    """Same inputs → identical outputs (pure function, no hidden state)."""
    inp = _inputs(
        asset_class_scores={"rates": 0.8},
        reason_code_scores={"central_bank": 0.9},
        prefilter_rule_score=0.7,
    )
    a = score(inp, taxonomy)
    b = score(inp, taxonomy)
    assert a == b


def test_component_scores_echo_inputs_verbatim(taxonomy) -> None:
    """Scalar inputs are echoed into component_scores untouched."""
    out = score(
        _inputs(
            prefilter_rule_score=0.42,
            domain_prior=0.81,
            source_type_prior=0.33,
            sentiment_confidence=0.27,
            entity_density=0.19,
        ),
        taxonomy,
    )
    cs = out.component_scores
    assert cs["prefilter_rule_score"] == pytest.approx(0.42)
    assert cs["domain_prior"] == pytest.approx(0.81)
    assert cs["source_type_prior"] == pytest.approx(0.33)
    assert cs["sentiment_confidence"] == pytest.approx(0.27)
    assert cs["entity_density"] == pytest.approx(0.19)
    assert cs["threshold_floor"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# Empty / all-zero input
# ---------------------------------------------------------------------------


def test_all_zero_input_scores_zero_and_not_relevant(taxonomy) -> None:
    out = score(_inputs(), taxonomy)
    assert out.finance_relevance_score == 0.0
    assert out.is_finance_relevant is False
    assert out.asset_classes_passed == ()
    assert out.reason_codes_passed == ()
    assert out.component_scores["asset_class_max"] == 0.0
    assert out.component_scores["reason_code_max"] == 0.0
    assert out.component_scores["negative_class_max"] == 0.0


def test_empty_score_maps_max_is_zero(taxonomy) -> None:
    """`_max({})` is 0.0, not an error — empty maps must be tolerated."""
    out = score(_inputs(asset_class_scores={}, reason_code_scores={}), taxonomy)
    assert out.component_scores["asset_class_max"] == 0.0
    assert out.component_scores["reason_code_max"] == 0.0


# ---------------------------------------------------------------------------
# Weighting formula
# ---------------------------------------------------------------------------


def test_raw_score_matches_documented_weighting(taxonomy) -> None:
    """The raw blend equals the exact documented linear combination."""
    out = score(
        _inputs(
            prefilter_rule_score=0.4,
            domain_prior=0.6,
            source_type_prior=0.8,
            asset_class_scores={"equities": 0.5},
            reason_code_scores={"earnings": 0.7},
            sentiment_confidence=0.2,
            entity_density=0.9,
        ),
        taxonomy,
    )
    expected = (
        0.30 * 0.5
        + 0.30 * 0.7
        + 0.15 * 0.4
        + 0.10 * 0.6
        + 0.05 * 0.8
        + 0.05 * 0.2
        + 0.05 * 0.9
    )
    assert out.finance_relevance_score == pytest.approx(expected)


def test_score_clamped_to_one_when_all_signals_max(taxonomy) -> None:
    """All-1.0 signals sum to 1.0 exactly and stay clamped to [0, 1]."""
    out = score(
        _inputs(
            prefilter_rule_score=1.0,
            domain_prior=1.0,
            source_type_prior=1.0,
            asset_class_scores={"equities": 1.0},
            reason_code_scores={"earnings": 1.0},
            sentiment_confidence=1.0,
            entity_density=1.0,
        ),
        taxonomy,
    )
    assert out.finance_relevance_score == pytest.approx(1.0)


def test_score_clamped_to_one_on_overflow_inputs(taxonomy) -> None:
    """Out-of-range (>1) inputs cannot push the final score above 1.0."""
    out = score(
        _inputs(
            prefilter_rule_score=5.0,
            domain_prior=5.0,
            source_type_prior=5.0,
            asset_class_scores={"equities": 5.0},
            reason_code_scores={"earnings": 5.0},
            sentiment_confidence=5.0,
            entity_density=5.0,
        ),
        taxonomy,
    )
    assert out.finance_relevance_score == 1.0


def test_score_clamped_to_zero_on_negative_inputs(taxonomy) -> None:
    """Negative inputs cannot push the final score below 0.0."""
    out = score(
        _inputs(
            prefilter_rule_score=-5.0,
            domain_prior=-5.0,
            source_type_prior=-5.0,
            sentiment_confidence=-5.0,
            entity_density=-5.0,
        ),
        taxonomy,
    )
    assert out.finance_relevance_score == 0.0
    assert out.is_finance_relevant is False


def test_max_picks_largest_asset_and_reason(taxonomy) -> None:
    """Only the single largest value per map contributes (max, not sum)."""
    out = score(
        _inputs(
            asset_class_scores={"a": 0.2, "b": 0.9, "c": 0.4},
            reason_code_scores={"x": 0.1, "y": 0.55},
        ),
        taxonomy,
    )
    assert out.component_scores["asset_class_max"] == pytest.approx(0.9)
    assert out.component_scores["reason_code_max"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# Floor / relevance boundary
# ---------------------------------------------------------------------------


def test_score_exactly_at_floor_is_relevant(taxonomy) -> None:
    """`final >= floor` is inclusive: a score == 0.35 is relevant.

    asset_max 0.35 + reason 0.40 -> 0.30*0.35 + 0.30*0.40 ... build a blend
    that lands exactly on the 0.35 floor. Use a single reason source so the
    asset-class gate (asset_max==0 needs reason>=0.75) does not interfere:
    asset_max here is 0.5 (>0) so the gate is inactive.
    """
    # raw = 0.30*asset + 0.30*reason; pick asset=0.5, reason=2/3 -> 0.15+0.20=0.35
    out = score(
        _inputs(
            asset_class_scores={"equities": 0.5},
            reason_code_scores={"earnings": 2.0 / 3.0},
        ),
        taxonomy,
    )
    assert out.finance_relevance_score == pytest.approx(0.35)
    assert out.is_finance_relevant is True


def test_score_just_below_floor_not_relevant(taxonomy) -> None:
    """A blend landing just under 0.35 is not relevant."""
    out = score(
        _inputs(asset_class_scores={"equities": 0.5}, reason_code_scores={"earnings": 0.6}),
        taxonomy,
    )
    # raw = 0.30*0.5 + 0.30*0.6 = 0.15 + 0.18 = 0.33 < 0.35
    assert out.finance_relevance_score == pytest.approx(0.33)
    assert out.is_finance_relevant is False


# ---------------------------------------------------------------------------
# Negative-class veto
# ---------------------------------------------------------------------------


def test_negative_veto_fires_when_block_met_and_signals_weak(taxonomy) -> None:
    """neg >= 0.65 with asset<0.5 and reason<0.5 → vetoed to not-relevant,
    even if the raw blend would otherwise clear the floor."""
    out = score(
        _inputs(
            prefilter_rule_score=1.0,
            domain_prior=1.0,
            source_type_prior=1.0,
            asset_class_scores={"equities": 0.49},
            reason_code_scores={"earnings": 0.49},
            negative_class_scores={"sports": 0.65},
        ),
        taxonomy,
    )
    assert out.finance_relevance_score >= 0.35  # would-be relevant
    assert out.is_finance_relevant is False  # but vetoed


def test_negative_veto_does_not_fire_when_asset_strong(taxonomy) -> None:
    """A strong asset signal (>=0.5) overrides the negative-class veto."""
    out = score(
        _inputs(
            asset_class_scores={"equities": 0.5},
            reason_code_scores={"earnings": 0.8},
            negative_class_scores={"sports": 0.99},
        ),
        taxonomy,
    )
    assert out.is_finance_relevant is True


def test_negative_veto_does_not_fire_when_reason_strong(taxonomy) -> None:
    """A strong reason signal (>=0.5) also overrides the negative-class veto.

    Keep reason at exactly 0.5 (the gate boundary) and lean on the priors to
    clear the 0.35 floor, so the *only* reason this could be non-relevant
    would be the veto — which must NOT fire because reason_max >= 0.5.
    """
    out = score(
        _inputs(
            prefilter_rule_score=1.0,
            domain_prior=1.0,
            source_type_prior=1.0,
            asset_class_scores={"equities": 0.4},  # < 0.5, so reason alone cancels veto
            reason_code_scores={"earnings": 0.5},
            negative_class_scores={"sports": 0.99},
        ),
        taxonomy,
    )
    # raw = 0.30*0.4 + 0.30*0.5 + 0.15 + 0.10 + 0.05 = 0.57 >= floor
    assert out.finance_relevance_score >= 0.35
    assert out.is_finance_relevant is True


def test_negative_below_block_does_not_veto(taxonomy) -> None:
    """neg just under 0.65 does NOT trigger the veto."""
    out = score(
        _inputs(
            asset_class_scores={"equities": 0.49},
            reason_code_scores={"earnings": 0.49},
            negative_class_scores={"sports": 0.64},
        ),
        taxonomy,
    )
    # raw = 0.30*0.49 + 0.30*0.49 = 0.294 < floor -> not relevant for floor reason,
    # but specifically NOT because of the veto. Bump priors to clear the floor:
    out2 = score(
        _inputs(
            prefilter_rule_score=1.0,
            domain_prior=1.0,
            source_type_prior=1.0,
            asset_class_scores={"equities": 0.49},
            reason_code_scores={"earnings": 0.49},
            negative_class_scores={"sports": 0.64},
        ),
        taxonomy,
    )
    assert out2.is_finance_relevant is True


# ---------------------------------------------------------------------------
# Asset-class gate (asset_max == 0 requires reason >= 0.75)
# ---------------------------------------------------------------------------


def test_zero_asset_with_weak_reason_blocked(taxonomy) -> None:
    """No asset hits + reason < 0.75 → not relevant (generic-noun guard)."""
    out = score(
        _inputs(
            prefilter_rule_score=1.0,
            domain_prior=1.0,
            source_type_prior=1.0,
            reason_code_scores={"lawsuit": 0.74},
            sentiment_confidence=1.0,
            entity_density=1.0,
        ),
        taxonomy,
    )
    assert out.component_scores["asset_class_max"] == 0.0
    assert out.is_finance_relevant is False


def test_zero_asset_with_strong_reason_passes_gate(taxonomy) -> None:
    """No asset hits but reason >= 0.75 survives the asset-class gate
    (provided the blend also clears the floor)."""
    out = score(
        _inputs(
            prefilter_rule_score=1.0,
            domain_prior=1.0,
            source_type_prior=1.0,
            reason_code_scores={"central_bank": 0.95},
        ),
        taxonomy,
    )
    assert out.component_scores["asset_class_max"] == 0.0
    # raw = 0.30*0.95 + 0.15 + 0.10 + 0.05 = 0.585 >= floor and reason>=0.75
    assert out.is_finance_relevant is True


def test_zero_asset_strong_reason_but_below_floor_still_blocked(taxonomy) -> None:
    """Gate passes (reason>=0.75) yet the floor is the binding constraint."""
    out = score(_inputs(reason_code_scores={"central_bank": 0.8}), taxonomy)
    # raw = 0.30 * 0.8 = 0.24 < 0.35 floor
    assert out.finance_relevance_score == pytest.approx(0.24)
    assert out.is_finance_relevant is False


# ---------------------------------------------------------------------------
# passed-label thresholds (asset_class_min / reason_code_min == 0.25)
# ---------------------------------------------------------------------------


def test_passed_labels_filtered_by_min_threshold(taxonomy) -> None:
    """Only labels >= 0.25 appear in *_passed tuples."""
    out = score(
        _inputs(
            asset_class_scores={"equities": 0.25, "rates": 0.24, "fx": 0.9},
            reason_code_scores={"earnings": 0.25, "guidance": 0.10},
        ),
        taxonomy,
    )
    assert set(out.asset_classes_passed) == {"equities", "fx"}
    assert "rates" not in out.asset_classes_passed
    assert set(out.reason_codes_passed) == {"earnings"}
    assert "guidance" not in out.reason_codes_passed


def test_passed_labels_empty_when_all_below_min(taxonomy) -> None:
    out = score(
        _inputs(
            asset_class_scores={"equities": 0.1},
            reason_code_scores={"earnings": 0.2},
        ),
        taxonomy,
    )
    assert out.asset_classes_passed == ()
    assert out.reason_codes_passed == ()


# ---------------------------------------------------------------------------
# Custom-threshold taxonomy paths (exercise taxonomy.threshold defaults)
# ---------------------------------------------------------------------------


def test_custom_floor_threshold_changes_relevance(tmp_path) -> None:
    """A higher finance_relevance_floor flips a borderline item to not-relevant."""
    import textwrap

    cfg = tmp_path / "tax.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            asset_classes:
              - {id: equities, hypothesis: "about equities"}
            impact_reason_codes:
              - {id: earnings, hypothesis: "about earnings"}
            negative_class:
              - {id: sports, hypothesis: "about sports"}
            thresholds:
              finance_relevance_floor: 0.90
              negative_class_block: 0.65
              asset_class_min: 0.25
              reason_code_min: 0.25
            domain_priors: {default: 0.45}
            source_type_priors: {default: 0.40}
            """
        ),
        encoding="utf-8",
    )
    tax = load_taxonomy(cfg)
    out = score(
        ScoringInputs(
            prefilter_rule_score=0.0,
            domain_prior=0.0,
            source_type_prior=0.0,
            asset_class_scores={"equities": 0.8},
            reason_code_scores={"earnings": 0.8},
            negative_class_scores={},
            sentiment_confidence=0.0,
            entity_density=0.0,
        ),
        tax,
    )
    # raw = 0.30*0.8 + 0.30*0.8 = 0.48 < 0.90 custom floor
    assert out.finance_relevance_score == pytest.approx(0.48)
    assert out.component_scores["threshold_floor"] == pytest.approx(0.90)
    assert out.is_finance_relevant is False


# ---------------------------------------------------------------------------
# estimate_entity_density
# ---------------------------------------------------------------------------


def test_entity_density_zero_length_is_zero() -> None:
    assert estimate_entity_density(5, 0) == 0.0
    assert estimate_entity_density(5, -10) == 0.0


def test_entity_density_zero_hits_is_zero() -> None:
    assert estimate_entity_density(0, 1000) == 0.0


def test_entity_density_known_value() -> None:
    """3 hits / 1000 chars -> density 3.0, /3.0 -> 1.0 (the cap boundary)."""
    assert estimate_entity_density(3, 1000) == pytest.approx(1.0)


def test_entity_density_below_cap() -> None:
    """1 hit / 1000 chars -> 1.0 hits-per-1k, /3.0 -> ~0.3333."""
    assert estimate_entity_density(1, 1000) == pytest.approx(1.0 / 3.0)


def test_entity_density_capped_at_one() -> None:
    """A dense text (many hits, short text) saturates at 1.0."""
    assert estimate_entity_density(100, 500) == 1.0


def test_entity_density_always_in_unit_interval() -> None:
    for hits, length in [(0, 1), (1, 1), (50, 10), (2, 5000), (10, 333)]:
        d = estimate_entity_density(hits, length)
        assert 0.0 <= d <= 1.0
