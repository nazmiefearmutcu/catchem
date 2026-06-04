"""Stage H: final scoring. Combines stage outputs into a single transparent
decision and the ``component_scores`` map persisted alongside.

This module deliberately does not import the stages — it operates on the
already-computed numeric features so it's trivially testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .taxonomy import Taxonomy


@dataclass(frozen=True)
class ScoringInputs:
    prefilter_rule_score: float          # 0..1
    domain_prior: float                  # 0..1
    source_type_prior: float             # 0..1
    asset_class_scores: Mapping[str, float]
    reason_code_scores: Mapping[str, float]
    negative_class_scores: Mapping[str, float]
    sentiment_confidence: float          # 0..1
    entity_density: float                # 0..1 (proxied from #hits / text length)


@dataclass(frozen=True)
class ScoringOutputs:
    is_finance_relevant: bool
    finance_relevance_score: float
    asset_classes_passed: tuple[str, ...]
    reason_codes_passed: tuple[str, ...]
    component_scores: Mapping[str, float]


def _max(values: Mapping[str, float]) -> float:
    return max(values.values()) if values else 0.0


def score(inputs: ScoringInputs, taxonomy: Taxonomy) -> ScoringOutputs:
    """Combine signals into a single transparent score.

    Heuristic-but-documented weighting:

        0.30 * max(asset_class)
      + 0.30 * max(reason_code)
      + 0.15 * prefilter_rule_score
      + 0.10 * domain_prior
      + 0.05 * source_type_prior
      + 0.05 * sentiment_confidence (only when sentiment is non-neutral)
      + 0.05 * entity_density

    Negative-class veto: if max(negative_class) ≥ negative_class_block,
    is_finance_relevant becomes False regardless of the rest.
    """
    asset_max = _max(inputs.asset_class_scores)
    reason_max = _max(inputs.reason_code_scores)
    negative_max = _max(inputs.negative_class_scores)

    raw = (
        0.30 * asset_max
        + 0.30 * reason_max
        + 0.15 * float(inputs.prefilter_rule_score)
        + 0.10 * float(inputs.domain_prior)
        + 0.05 * float(inputs.source_type_prior)
        + 0.05 * float(inputs.sentiment_confidence)
        + 0.05 * float(inputs.entity_density)
    )
    final = max(0.0, min(1.0, raw))

    floor = taxonomy.threshold("finance_relevance_floor", 0.35)
    neg_block = taxonomy.threshold("negative_class_block", 0.65)

    relevant = final >= floor
    # Negative-class veto: clearly non-finance items.
    if negative_max >= neg_block and asset_max < 0.5 and reason_max < 0.5:
        relevant = False
    # Asset-class gate: with zero asset-class hits, require a very strong reason
    # to be considered finance-relevant. Prevents proper-noun-rich generic news
    # (e.g. art looted by Nazis) from sneaking past on a single high reason code.
    if asset_max == 0.0 and reason_max < 0.75:
        relevant = False

    ac_min = taxonomy.threshold("asset_class_min", 0.25)
    rc_min = taxonomy.threshold("reason_code_min", 0.25)
    asset_passed = tuple(k for k, v in inputs.asset_class_scores.items() if v >= ac_min)
    reason_passed = tuple(k for k, v in inputs.reason_code_scores.items() if v >= rc_min)

    components: dict[str, float] = {
        "asset_class_max": float(asset_max),
        "reason_code_max": float(reason_max),
        "negative_class_max": float(negative_max),
        "prefilter_rule_score": float(inputs.prefilter_rule_score),
        "domain_prior": float(inputs.domain_prior),
        "source_type_prior": float(inputs.source_type_prior),
        "sentiment_confidence": float(inputs.sentiment_confidence),
        "entity_density": float(inputs.entity_density),
        "raw_relevance_score": float(final),
        "threshold_floor": float(floor),
    }
    return ScoringOutputs(
        is_finance_relevant=bool(relevant),
        finance_relevance_score=float(final),
        asset_classes_passed=asset_passed,
        reason_codes_passed=reason_passed,
        component_scores=components,
    )


def estimate_entity_density(num_hits: int, text_length: int) -> float:
    """Hits per 1000 chars, capped at 1.0."""
    if text_length <= 0:
        return 0.0
    density = num_hits / (text_length / 1000.0)
    return float(max(0.0, min(1.0, density / 3.0)))
