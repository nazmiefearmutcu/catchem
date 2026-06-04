"""Stage C: finance sentiment.

Stub uses a small finance polarity lexicon. ML path wraps FinBERT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .schemas import AwarenessCaptureView, SentimentLabel


@dataclass(frozen=True)
class SentimentResult:
    label: SentimentLabel
    score: float                 # 0..1 confidence
    model_version: str


class SentimentClassifier(Protocol):
    def classify(self, cap: AwarenessCaptureView) -> SentimentResult: ...


_POS_TERMS = (
    "beat", "beats", "exceeded", "surge", "rally", "outperform", "upgrade", "upgraded",
    "raise", "raised", "raises", "boom", "growth", "gain", "gains", "strong", "robust",
    "record high", "record-high", "record profit", "buyback", "expansion", "expanded",
    "tailwind", "bullish",
)
_NEG_TERMS = (
    "miss", "missed", "fell", "slump", "plunge", "sink", "downgrade", "cut", "cuts",
    "lower", "loss", "losses", "weak", "warn", "warns", "warning", "default", "bankrupt",
    "recession", "headwind", "bearish", "shrink", "shrank", "fraud", "lawsuit", "probe",
)


# BUG-FF: pre-fix the stub used `t in text` substring matching, which made
# any English word containing a term-as-substring fire the wrong polarity:
#   "miss"  → dismiss / mission / submission / transmission  (false negative)
#   "fell"  → fellowship / fellow / felled                   (false negative)
#   "lower" → follower / flowering                            (false negative)
#   "loss"  → glossary                                        (false negative)
#   "weak"  → tweak                                           (false negative)
#   "cut"   → executor / cutting                              (false negative)
#   "raise" → fundraiser (only if exact "fundraise" not "fundraising")
# Pre-compiled word-boundary regexes are O(N*M) like the substring check
# but with O(N) compilation amortized over the process lifetime — and we
# need `re.escape` anyway for multi-word terms ("record high", "record-high")
# whose internal whitespace/hyphen would break a naive `\b` flank.
def _compile(terms: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(
        re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])")
        for t in terms
    )


_POS_PATTERNS = _compile(_POS_TERMS)
_NEG_PATTERNS = _compile(_NEG_TERMS)


class SentimentStub:
    model_version = "stub-sentiment/v1"

    @staticmethod
    def classify(cap: AwarenessCaptureView) -> SentimentResult:
        text = ((cap.title or "") + " " + (cap.text or "")[:3000]).lower()
        pos = sum(1 for p in _POS_PATTERNS if p.search(text))
        neg = sum(1 for p in _NEG_PATTERNS if p.search(text))
        if pos == 0 and neg == 0:
            return SentimentResult(label=SentimentLabel.NEUTRAL, score=0.5, model_version="stub-sentiment/v1")
        if pos > neg:
            score = min(1.0, 0.5 + 0.1 * (pos - neg))
            return SentimentResult(label=SentimentLabel.POSITIVE, score=score, model_version="stub-sentiment/v1")
        if neg > pos:
            score = min(1.0, 0.5 + 0.1 * (neg - pos))
            return SentimentResult(label=SentimentLabel.NEGATIVE, score=score, model_version="stub-sentiment/v1")
        return SentimentResult(label=SentimentLabel.NEUTRAL, score=0.55, model_version="stub-sentiment/v1")


class SentimentModel:
    """Wraps a HF text-classification pipeline."""

    def __init__(self, model_name: str = "ProsusAI/finbert") -> None:
        from transformers import pipeline  # type: ignore[import-not-found]

        self.model_name = model_name
        self._pipe = pipeline("text-classification", model=model_name, device=-1, top_k=None)

    @property
    def model_version(self) -> str:
        return f"hf:{self.model_name}"

    def classify(self, cap: AwarenessCaptureView) -> SentimentResult:
        text = (cap.title or "") + "\n" + (cap.text or "")[:1500]
        if not text.strip():
            return SentimentResult(label=SentimentLabel.UNKNOWN, score=0.0, model_version=self.model_version)
        out = self._pipe(text, truncation=True)
        if not out:
            return SentimentResult(label=SentimentLabel.NEUTRAL, score=0.5, model_version=self.model_version)
        scored = out[0]
        if isinstance(scored, list):
            scored = sorted(scored, key=lambda x: x["score"], reverse=True)
            top = scored[0]
        else:
            top = scored
        lbl_raw = str(top.get("label", "")).lower()
        score = float(top.get("score", 0.5))
        if "pos" in lbl_raw:
            return SentimentResult(label=SentimentLabel.POSITIVE, score=score, model_version=self.model_version)
        if "neg" in lbl_raw:
            return SentimentResult(label=SentimentLabel.NEGATIVE, score=score, model_version=self.model_version)
        return SentimentResult(label=SentimentLabel.NEUTRAL, score=score, model_version=self.model_version)


def make_sentiment(model_name: str, use_stub: bool) -> SentimentClassifier:
    if use_stub:
        return SentimentStub()
    try:
        return SentimentModel(model_name=model_name)
    except Exception:
        return SentimentStub()
