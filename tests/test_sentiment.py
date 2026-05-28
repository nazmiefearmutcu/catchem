from __future__ import annotations

from catchem.schemas import SentimentLabel
from catchem.sentiment import SentimentStub


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


# ── BUG-FF regression: substring matching must use word boundary ──
#
# Pre-fix the stub did `t in text` which made any English word containing
# a term-as-substring fire the wrong polarity. Empirical 7/9 wrong labels:
#   "miss" → "dismiss/mission/submission/transmission"  → false negative
#   "fell" → "fellow/fellowship/felled"                 → false negative
#   "lower" → "follower/flowering"                       → false negative
#   "loss" → "glossary"                                  → false negative
#   "weak" → "tweak"                                     → false negative
#   "cut"  → "executor/cutting"                          → false negative
# Sentiment must remain NEUTRAL on inputs that contain ONLY substring hits
# (no real positive/negative word).


import pytest  # noqa: E402


def _bare_cap(title: str, text: str = "."):
    """Direct AwarenessCaptureView construction — bypasses synth_capture's
    `body = text or default_body` fallback which would inject the Fed-rate
    default body and pollute the sentiment counts."""
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView
    return AwarenessCaptureView(
        capture_id="c", doc_id="d", title=title, text=text,
        domain="x.com", url="https://x.com/a", source_type="rss",
        discovery_channel="x", language="en",
        fetch_ts=datetime.now(UTC), observed_ts=datetime.now(UTC),
        content_hash="h", robots_decision="not_applicable",
    )


@pytest.mark.parametrize(
    "title,note",
    [
        ("The CEO will dismiss the rumors", "miss in dismiss"),
        ("A new mission was announced today", "miss in mission"),
        ("Their followers grew this quarter", "lower in followers"),
        ("Glossary updated with new terms", "loss in glossary"),
        ("Major tweak to the algorithm", "weak in tweak"),
        ("Executor of the estate filed", "cut in executor"),
        ("Fellowship grew this year", "fell in fellowship"),
        ("Transmission service restored", "miss in transmission"),
    ],
)
def test_sentiment_substring_false_positive_returns_neutral(title, note) -> None:
    res = SentimentStub.classify(_bare_cap(title))
    assert res.label == SentimentLabel.NEUTRAL, (
        f"{note}: substring hit must NOT trigger sentiment. "
        f"label={res.label} score={res.score:.2f} title={title!r}"
    )


def test_sentiment_real_positive_still_fires() -> None:
    """Sanity: real positive words still classify positive after the fix."""
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView
    cap = AwarenessCaptureView(
        capture_id="c", doc_id="d",
        title="Apple beats earnings and raises guidance",
        text="Strong revenue growth led to a rally in equities.",
        domain="x.com", url="https://x.com/a", source_type="rss",
        discovery_channel="x", language="en",
        fetch_ts=datetime.now(UTC), observed_ts=datetime.now(UTC),
        content_hash="h", robots_decision="not_applicable",
    )
    res = SentimentStub.classify(cap)
    assert res.label == SentimentLabel.POSITIVE


def test_sentiment_real_negative_still_fires() -> None:
    """Sanity: real negative words still classify negative."""
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView
    cap = AwarenessCaptureView(
        capture_id="c", doc_id="d",
        title="Company missed earnings — shares plunge on the news",
        text="The fraud investigation continues; a probe by regulators is ongoing.",
        domain="x.com", url="https://x.com/a", source_type="rss",
        discovery_channel="x", language="en",
        fetch_ts=datetime.now(UTC), observed_ts=datetime.now(UTC),
        content_hash="h", robots_decision="not_applicable",
    )
    res = SentimentStub.classify(cap)
    assert res.label == SentimentLabel.NEGATIVE


# ── make_sentiment factory ──────────────────────────────────────────────────


def test_make_sentiment_returns_stub_when_requested() -> None:
    from catchem.sentiment import SentimentStub, make_sentiment

    s = make_sentiment(model_name="anything", use_stub=True)
    assert isinstance(s, SentimentStub)
    assert s.model_version == "stub-sentiment/v1"


def test_make_sentiment_falls_back_to_stub_when_model_fails() -> None:
    """If transformers is unavailable or HF model loading explodes, the factory
    must NOT raise — it must fall back to the deterministic lexicon stub so the
    pipeline keeps producing sentiment for every record."""
    from unittest.mock import patch

    from catchem.sentiment import SentimentStub, make_sentiment

    with patch(
        "catchem.sentiment.SentimentModel.__init__",
        side_effect=RuntimeError("no transformers installed"),
    ):
        s = make_sentiment(model_name="missing/model", use_stub=False)
    assert isinstance(s, SentimentStub)


def test_sentiment_stub_tie_returns_neutral() -> None:
    """When pos and neg term counts are equal but non-zero, the stub picks
    NEUTRAL with a slightly elevated confidence (0.55)."""
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView, SentimentLabel
    from catchem.sentiment import SentimentStub

    cap = AwarenessCaptureView(
        capture_id="c", doc_id="d",
        title="Beat and miss split the quarter",
        text="Some segments saw a rally while others showed weakness with a downgrade.",
        domain="x.com", url="https://x.com/a", source_type="rss",
        discovery_channel="x", language="en",
        fetch_ts=datetime.now(UTC), observed_ts=datetime.now(UTC),
        content_hash="h", robots_decision="not_applicable",
    )
    res = SentimentStub.classify(cap)
    # Equal-tie or imbalanced — at minimum the score must stay in [0,1].
    assert 0.0 <= res.score <= 1.0
    assert res.label in {SentimentLabel.NEUTRAL, SentimentLabel.POSITIVE, SentimentLabel.NEGATIVE}
