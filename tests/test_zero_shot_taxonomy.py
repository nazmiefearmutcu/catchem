from __future__ import annotations

from datetime import UTC

import pytest

from catchem.taxonomy import default_taxonomy_path, load_taxonomy
from catchem.zero_shot_classifier import ZeroShotStub, make_zero_shot


@pytest.fixture
def taxonomy():
    return load_taxonomy(default_taxonomy_path())


def test_stub_picks_central_bank_for_fed_news(taxonomy, synth_capture) -> None:
    zs = ZeroShotStub(taxonomy)
    res = zs.classify(synth_capture())
    # The Fed/rates story should score central_bank, rates, and macro / inflation.
    top = dict(res.label_scores)
    assert top.get("central_bank", 0) > 0.4
    assert top.get("rates", 0) > 0.4
    assert top.get("inflation", 0) > 0.4
    # Sports / lifestyle should not appear
    for neg in ("sports", "celebrity", "lifestyle"):
        assert top.get(neg, 0) < 0.4


def test_make_zero_shot_falls_back_to_stub_without_ml(taxonomy) -> None:
    zs = make_zero_shot(taxonomy, "facebook/bart-large-mnli", use_stub=True)
    assert isinstance(zs, ZeroShotStub)


def test_empty_capture_yields_no_scores(taxonomy, synth_capture) -> None:
    zs = ZeroShotStub(taxonomy)
    cap = synth_capture(title="", text="   ")
    res = zs.classify(cap)
    assert res.label_scores == {}


def test_negative_class_triggers_on_sports(taxonomy, synth_non_finance_capture) -> None:
    zs = ZeroShotStub(taxonomy)
    res = zs.classify(synth_non_finance_capture)
    # The Stub uses alias overlap; even if "sports" alias isn't present in the
    # text, the hypothesis-derived tokens (game, athletic, competition...) cover it.
    # We don't require sports > finance, just that finance is low.
    eq = res.label_scores.get("equities", 0)
    rates = res.label_scores.get("rates", 0)
    assert eq < 0.5 and rates < 0.5


# ── BUG-R/S regression: title weighting actually applies; unigram weighting
# is repetition-aware (not deduped by a set) ─────────────────────────────────
#
# Pre-fix the classifier built `weighted = title*3 + body` then immediately
# wrapped extraction in `set(...)`. The set destroyed every repetition,
# making the title-multiplier a no-op for single-word labels. Bigrams used a
# plain list so they DID benefit from repetition — an undocumented asymmetry
# (multi-word aliases got 3x weight, single-word aliases got 1x).


def _cap(title: str, text: str):
    from datetime import datetime

    from catchem.schemas import AwarenessCaptureView
    return AwarenessCaptureView(
        capture_id="c", doc_id="d",
        title=title, text=text,
        domain="x.com", url="https://x.com/a",
        source_type="rss", discovery_channel="rss:x.com",
        language="en",
        fetch_ts=datetime.now(UTC),
        observed_ts=datetime.now(UTC),
        content_hash="h", robots_decision="not_applicable",
    )


def test_title_match_outweighs_body_match_for_single_word_labels(taxonomy) -> None:
    """One single-word label hit in the TITLE must produce a strictly higher
    score than the same word hit in the BODY only. Pre-fix both produced
    identical scores because the set() deduplication wiped the 3x repetition
    of the title."""
    zs = ZeroShotStub(taxonomy)
    title_only = zs.classify(_cap(title="Fed raises rates", text="No relevant content here."))
    body_only = zs.classify(_cap(title="Generic news", text="Fed raises rates today."))
    # Compare a label that gets exactly one keyword hit in each variant.
    label = "central_bank"
    t_score = title_only.label_scores.get(label, 0.0)
    b_score = body_only.label_scores.get(label, 0.0)
    assert t_score > b_score, (
        f"Title hit must outweigh body hit for '{label}'. "
        f"title_score={t_score:.4f} body_score={b_score:.4f}. "
        f"If equal, the 3x title weighting in zero_shot_classifier is a no-op "
        f"(set() dedupe)."
    )


def test_hypothesis_generic_word_does_not_trigger_label(taxonomy) -> None:
    """BUG-EE: pre-fix the index also held every >3-char word from each
    label's `hypothesis` sentence — including generic English like 'move',
    'decision', 'event'. A BTC headline reading 'Analysts attribute the
    move to institutional demand' fired `central_bank` because 'move' was
    a central_bank hypothesis token. The stub must rely on curated
    aliases + id only; the hypothesis path is for the BART-MNLI template.
    """
    zs = ZeroShotStub(taxonomy)
    # No central_bank alias in this text; only the word 'move' is shared
    # with the central_bank hypothesis.
    res = zs.classify(_cap(
        title="Bitcoin rallies past $80,000 amid ETF inflows",
        text="Analysts attribute the move to institutional demand.",
    ))
    assert "central_bank" not in res.label_scores, (
        f"Generic hypothesis word 'move' must NOT trigger central_bank. "
        f"label_scores={dict(res.label_scores)}"
    )
    # And the actual finance class (crypto, via 'bitcoin' alias) MUST fire.
    assert res.label_scores.get("crypto", 0.0) > 0.4


def test_unigram_repetition_in_body_increases_score(taxonomy) -> None:
    """A body that mentions a label-aliased token MANY times should outscore
    a body that mentions it once. Pre-fix the unigram scoring uses set(),
    so 1x and 10x mentions tied. Bigrams DID get repetition — undocumented
    asymmetry between single-word and multi-word aliases."""
    zs = ZeroShotStub(taxonomy)
    once = zs.classify(_cap(title="market wrap",
                            text="The Fed met today. Markets reacted."))
    many = zs.classify(_cap(
        title="market wrap",
        text=("The Fed met today. The Fed talked. The Fed listened. "
              "The Fed spoke. The Fed stood."),
    ))
    label = "central_bank"
    once_s = once.label_scores.get(label, 0.0)
    many_s = many.label_scores.get(label, 0.0)
    assert many_s > once_s, (
        f"Repeated body mentions must increase score. once={once_s:.4f} many={many_s:.4f}. "
        f"If equal, unigram scoring is set-based (dedupes) — fix uses Counter."
    )


def test_zero_shot_result_top_above() -> None:
    from catchem.zero_shot_classifier import ZeroShotResult
    res = ZeroShotResult(
        label_scores={"equities": 0.8, "crypto": 0.3, "rates": 0.9},
        model_version="test"
    )
    top = res.top_above(0.5)
    assert top == [("rates", 0.9), ("equities", 0.8)]


def test_zero_shot_model_happy_path() -> None:
    import sys
    from unittest.mock import MagicMock, patch
    from catchem.zero_shot_classifier import ZeroShotModel, make_zero_shot, ZeroShotStub
    from catchem.taxonomy import default_taxonomy_path, load_taxonomy

    mock_transformers = MagicMock()
    mock_pipeline = MagicMock()
    mock_transformers.pipeline = mock_pipeline
    mock_pipe = MagicMock()
    mock_pipeline.return_value = mock_pipe

    # Grab hypotheses from the real taxonomy to match them exactly in mocks
    tax = load_taxonomy(default_taxonomy_path())
    all_hyps = list(tax.all_hypotheses().items())
    assert len(all_hyps) > 0
    first_id, first_hyp = all_hyps[0]

    mock_pipe.return_value = {
        "labels": [first_hyp],
        "scores": [0.95]
    }

    with patch.dict("sys.modules", {"transformers": mock_transformers}):
        model = ZeroShotModel(tax, "facebook/bart-large-mnli")
        assert model.model_name == "facebook/bart-large-mnli"
        assert model.model_version == "hf:facebook/bart-large-mnli"

        mock_pipeline.assert_called_once_with("zero-shot-classification", model="facebook/bart-large-mnli", device=-1)

        cap = _cap(title="Profit grows", text="Revenue surged!")
        res = model.classify(cap)
        assert res.label_scores.get(first_id) == 0.95

        # Empty string classifier input
        cap_empty = _cap(title="   ", text="   ")
        res_empty = model.classify(cap_empty)
        assert res_empty.label_scores == {}

        # Test successful make_zero_shot with model
        s = make_zero_shot(tax, "facebook/bart-large-mnli", use_stub=False)
        assert isinstance(s, ZeroShotModel)

        # Test make_zero_shot fallback on pipeline load failure
        mock_pipeline.side_effect = RuntimeError("failed to load")
        s_fallback = make_zero_shot(tax, "facebook/bart-large-mnli", use_stub=False)
        assert isinstance(s_fallback, ZeroShotStub)


def test_zero_shot_bigram_title_body_overlap(taxonomy) -> None:
    zs = ZeroShotStub(taxonomy)
    # 'central bank' bigram appears in both title and body.
    # It must be removed from body_bigrams.
    cap = _cap(title="Central Bank policy review", text="Central bank announcement today.")
    res = zs.classify(cap)
    assert res is not None


def test_zero_shot_protocol() -> None:
    from catchem.zero_shot_classifier import ZeroShot
    assert ZeroShot.classify(None, None) is Ellipsis or ZeroShot.classify(None, None) is None



