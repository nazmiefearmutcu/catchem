"""Scoring and evidence units."""

from __future__ import annotations

from fusion_stack.evidence import build_reason_text, extract_evidence, split_sentences
from fusion_stack.schemas import AwarenessCaptureView
from fusion_stack.scoring import ScoringInputs, estimate_entity_density, score
from fusion_stack.taxonomy import default_taxonomy_path, load_taxonomy


def test_sentence_split_basic() -> None:
    sents = split_sentences("This is one. This is two? And a third!")
    assert len(sents) >= 3


def test_evidence_picks_label_relevant_sentence(synth_capture) -> None:
    cap = synth_capture()
    evidence = extract_evidence(cap, label_terms=["rates", "fed"], entity_terms=["powell"], top_k=2)
    assert any("rate" in e.lower() or "fed" in e.lower() for e in evidence)


def test_reason_text_compact() -> None:
    text = build_reason_text(["rates"], ["central_bank"], "negative")
    assert text == "rates | central_bank | sentiment=negative"


def test_score_respects_floor() -> None:
    tax = load_taxonomy(default_taxonomy_path())
    out = score(
        ScoringInputs(
            prefilter_rule_score=0.0,
            domain_prior=0.0,
            source_type_prior=0.0,
            asset_class_scores={},
            reason_code_scores={},
            negative_class_scores={},
            sentiment_confidence=0.0,
            entity_density=0.0,
        ),
        taxonomy=tax,
    )
    assert out.is_finance_relevant is False
    assert out.finance_relevance_score == 0.0


def test_score_marks_relevant_when_signals_present() -> None:
    tax = load_taxonomy(default_taxonomy_path())
    out = score(
        ScoringInputs(
            prefilter_rule_score=0.7,
            domain_prior=0.9,
            source_type_prior=0.55,
            asset_class_scores={"rates": 0.8, "macro": 0.5},
            reason_code_scores={"central_bank": 0.9},
            negative_class_scores={},
            sentiment_confidence=0.6,
            entity_density=0.3,
        ),
        taxonomy=tax,
    )
    assert out.is_finance_relevant is True
    assert out.finance_relevance_score > 0.5
    assert "rates" in out.asset_classes_passed
    assert "central_bank" in out.reason_codes_passed


def test_negative_class_block_vetoes_borderline() -> None:
    tax = load_taxonomy(default_taxonomy_path())
    out = score(
        ScoringInputs(
            prefilter_rule_score=0.4,
            domain_prior=0.05,
            source_type_prior=0.4,
            asset_class_scores={"equities": 0.3},
            reason_code_scores={"earnings": 0.3},
            negative_class_scores={"sports": 0.95},
            sentiment_confidence=0.0,
            entity_density=0.0,
        ),
        taxonomy=tax,
    )
    assert out.is_finance_relevant is False


def test_entity_density_clamped() -> None:
    assert estimate_entity_density(0, 0) == 0.0
    assert 0 <= estimate_entity_density(20, 1000) <= 1.0
