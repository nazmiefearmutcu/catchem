from __future__ import annotations

from fusion_stack.schemas import SentimentLabel
from fusion_stack.sentiment import SentimentStub


def test_positive_terms_yield_positive(synth_capture) -> None:
    cap = synth_capture(title="Apple beats earnings, raises guidance",
                        text="Apple beat consensus and raised guidance. Stock surge expected.")
    res = SentimentStub.classify(cap)
    assert res.label == SentimentLabel.POSITIVE
    assert res.score > 0.5


def test_negative_terms_yield_negative(synth_capture) -> None:
    cap = synth_capture(title="Boeing warns on Q4 — lawsuit and downgrade",
                        text="The company missed estimates, faces a downgrade, and a lawsuit looms.")
    res = SentimentStub.classify(cap)
    assert res.label == SentimentLabel.NEGATIVE


def test_neutral_when_no_signals(synth_capture) -> None:
    cap = synth_capture(title="Quarterly update issued", text="The company issued an update today.")
    res = SentimentStub.classify(cap)
    assert res.label == SentimentLabel.NEUTRAL
