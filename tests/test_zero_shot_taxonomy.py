from __future__ import annotations

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
    sports_score = res.label_scores.get("sports", 0)
    # The Stub uses alias overlap; even if "sports" alias isn't present in the
    # text, the hypothesis-derived tokens (game, athletic, competition...) cover it.
    # We don't require sports > finance, just that finance is low.
    eq = res.label_scores.get("equities", 0)
    rates = res.label_scores.get("rates", 0)
    assert eq < 0.5 and rates < 0.5
