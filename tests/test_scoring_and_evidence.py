"""Scoring and evidence units."""

from __future__ import annotations

from datetime import UTC

from catchem.evidence import build_reason_text, extract_evidence, split_sentences
from catchem.schemas import AwarenessCaptureView
from catchem.scoring import ScoringInputs, estimate_entity_density, score
from catchem.taxonomy import default_taxonomy_path, load_taxonomy


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


# ── BUG-W: evidence sentence scoring must use word boundaries, not substring ──
#
# Pre-fix: `sum(1.0 for t in terms if t in s_lc)` counted any substring hit,
# so the term "rate" matched "operating", "fed" matched "federated", "cut"
# matched "executed". A sentence with zero real overlap could outscore one
# with the actual label match.


def _cap(title: str, text: str) -> AwarenessCaptureView:
    """Minimal AwarenessCaptureView builder for evidence tests."""
    from datetime import datetime

    return AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        title=title,
        text=text,
        domain="x.com",
        url="https://x.com/a",
        source_type="rss",
        discovery_channel="rss:x.com",
        language="en",
        fetch_ts=datetime.now(UTC),
        observed_ts=datetime.now(UTC),
        content_hash="h",
        robots_decision="not_applicable",
    )


def test_evidence_substring_match_false_positive_not_counted() -> None:
    """Term 'rate' must NOT match 'operating' (substring 'rate' inside the
    word 'operating'). Sentence with REAL 'rate' hit must rank above the
    sentence with only spurious substring hits.

    Without the fix the two sentences score identically — both contain the
    bytes 'rate' — and ranking is decided by tie-breaker rules instead of
    actual relevance.
    """
    cap = _cap(
        title="Daily ops update",
        text=(
            "The company is operating its data centres at capacity. "
            "Our base interest rate decision is due next week."
        ),
    )
    ev = extract_evidence(cap, label_terms=["rate"], entity_terms=[], top_k=2)
    # The real-hit sentence MUST come before the spurious-hit one.
    real_idx = next(i for i, s in enumerate(ev) if "interest rate decision" in s.lower())
    operating_present = any("operating its data centres" in s.lower() for s in ev)
    # The real hit must appear; if 'operating' also appears it must be AFTER.
    assert real_idx == 0 or not operating_present, (
        f"Real 'rate' hit must outrank 'operating' substring hit. ev={ev}"
    )


def test_channel_mapper_equities_with_no_reason_has_general_channel() -> None:
    """BUG-Z: equities was the only asset class without a `*` wildcard rule.
    A record `asset=[equities]` with no recognised reason code returned
    `channels=[]`, which broke the downstream routing UI on edge-case
    inputs (e.g. a vague equities mention without an explicit reason)."""
    from catchem.channel_mapper import map_channels

    channels = map_channels(asset_classes=["equities"], reason_codes=[])
    assert "equities.general" in channels, (
        f"Equities + no reason should land in equities.general. Got: {channels}"
    )


def test_channel_mapper_specific_equities_rule_still_wins() -> None:
    """The fallback wildcard must NOT mask the specific (asset, reason) rules."""
    from catchem.channel_mapper import map_channels

    channels = map_channels(asset_classes=["equities"], reason_codes=["earnings"])
    # Both fire, but the specific channel must be present.
    assert "equities.earnings" in channels
    assert "equities.general" in channels  # wildcard fallback too


def test_horizons_cover_every_taxonomy_reason_code() -> None:
    """BUG-AA: every reason code in the taxonomy MUST be mapped to at least
    one horizon — silent gaps (originally `product_launch`) produce
    records with `impact_horizons=[]` that downstream UI can't sort."""
    from catchem.service import _horizon_buckets
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    taxonomy = load_taxonomy(default_taxonomy_path())
    short_term, one_week, structural = _horizon_buckets()
    all_buckets = short_term | one_week | structural
    missing = {r for r in taxonomy.reason_code_ids if r not in all_buckets}
    assert not missing, (
        f"Reason code(s) not mapped to any horizon bucket: {sorted(missing)}. "
        f"Update _horizons_from_reasons() in service.py."
    )


def test_evidence_fed_does_not_match_federated() -> None:
    """Term 'fed' must NOT match the word 'federated'. Pre-fix the substring
    match would score the 'federated' sentence equal to a real Fed sentence.
    """
    cap = _cap(
        title="State news",
        text=("A federated identity system is being adopted. The Fed cut rates by 25 bps today."),
    )
    ev = extract_evidence(cap, label_terms=["fed"], entity_terms=[], top_k=2)
    real_idx = next(i for i, s in enumerate(ev) if "Fed cut rates" in s)
    spurious_present = any("federated identity" in s.lower() for s in ev)
    assert real_idx == 0 or not spurious_present, (
        f"Real 'Fed' hit must outrank 'federated' substring hit. ev={ev}"
    )


def test_clean_boilerplate_text() -> None:
    from catchem.evidence import clean_boilerplate_text

    text = "This is a real sentence. Follow us on Twitter for more updates. Click here to read more."
    cleaned = clean_boilerplate_text(text)
    assert cleaned == "This is a real sentence."


def test_split_sentences_edge_cases() -> None:
    # empty/None/whitespace
    assert split_sentences(None) == []
    assert split_sentences("   ") == []
    assert split_sentences("") == []

    # consecutive whitespace resulting in empty sentences
    assert len(split_sentences("Sentence one.   . Sentence two.")) == 2

    # boilerplate removal in split_sentences
    assert split_sentences("Sentence one. All rights reserved.") == ["Sentence one."]

    # long sentence truncation (> 400 chars)
    long_sent = "A" * 399 + " B" * 10
    res = split_sentences(long_sent)
    assert len(res) == 1
    assert res[0].endswith("…")
    assert len(res[0]) <= 401

    # duplicates seen
    assert split_sentences("Hello. Hello. Hello.") == ["Hello."]


def test_split_sentences_empty_part_mock() -> None:
    from unittest.mock import patch

    with patch("catchem.evidence._SENTENCE_SPLIT_RE") as mock_re:
        mock_re.split.return_value = ["", "Valid sentence"]
        assert split_sentences("some text") == ["Valid sentence"]


def test_sentence_word_match_empty_term() -> None:
    from catchem.evidence import _sentence_word_match

    assert _sentence_word_match("hello world", "") is False
    assert _sentence_word_match("hello world", "world") is True
    assert _sentence_word_match("hello world", "nonexistent") is False


def test_extract_evidence_edge_cases() -> None:
    # no title, no body -> no sentences -> return []
    cap_empty = _cap(title="", text="")
    assert extract_evidence(cap_empty, label_terms=["fed"], entity_terms=[]) == []

    # no terms -> fallback to first sentence
    cap_no_terms = _cap(title="Fallback title", text="Body text.")
    assert extract_evidence(cap_no_terms, label_terms=[], entity_terms=[]) == ["Fallback title"]

    # title identical to first body sentence (s in seen branch)
    cap_duplicate = _cap(title="Same Title.", text="Same Title. Another sentence.")
    ev = extract_evidence(cap_duplicate, label_terms=["same"], entity_terms=[])
    assert ev == ["Same Title."]

    # no out: sentences exist, terms exist, but no matches -> returns first sentence
    cap_no_match = _cap(title="", text="Hello world. Beautiful day.")
    assert extract_evidence(cap_no_match, label_terms=["nonexistent"], entity_terms=[]) == ["Hello world."]


def test_build_reason_text_edge_cases() -> None:
    # empty asset/reason and missing sentiment
    assert build_reason_text([], [], None) == "general | no-specific-reason | sentiment=unknown"
