from __future__ import annotations

import pytest

from fusion_stack.finance_filter import FastPrefilter
from fusion_stack.taxonomy import default_taxonomy_path, load_taxonomy


@pytest.fixture
def prefilter() -> FastPrefilter:
    return FastPrefilter(taxonomy=load_taxonomy(default_taxonomy_path()))


def test_finance_capture_keeps(prefilter, synth_capture) -> None:
    res = prefilter.evaluate(synth_capture())
    assert res.keep is True
    assert res.rule_score >= 0.3
    assert "fed" in {k.lower() for k in res.matched_keywords} or "rate" in res.matched_keywords or "rates" in res.matched_keywords


def test_obvious_sports_is_rejected(prefilter, synth_non_finance_capture) -> None:
    res = prefilter.evaluate(synth_non_finance_capture)
    # ESPN with sports keywords and no finance hits → conservative reject
    assert res.keep is False
    assert res.rule_score < 0.10


def test_cashtag_signal_is_captured(prefilter, synth_capture) -> None:
    cap = synth_capture(title="$AAPL beats on earnings", text="$AAPL beat earnings expectations.")
    res = prefilter.evaluate(cap)
    assert "cashtag" in res.matched_keywords


def test_unknown_domain_uses_default_prior(prefilter, synth_capture) -> None:
    cap = synth_capture(domain="nowhere.example.com")
    res = prefilter.evaluate(cap)
    # default prior is 0.45 — present in component
    assert 0.30 <= res.domain_prior <= 0.5
