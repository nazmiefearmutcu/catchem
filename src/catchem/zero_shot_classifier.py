"""Stage B: multi-label zero-shot taxonomy classification.

Two implementations behind one interface:
  * ``ZeroShotStub``: deterministic, CPU-friendly. Uses alias/keyword overlap +
    title weighting. Designed to make taxonomy tests stable without needing the
    HF model.
  * ``ZeroShotModel``: wraps ``transformers.pipeline("zero-shot-classification")``
    with facebook/bart-large-mnli.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

from .schemas import AwarenessCaptureView
from .taxonomy import Taxonomy


@dataclass(frozen=True)
class ZeroShotResult:
    label_scores: Mapping[str, float]
    model_version: str

    def top_above(self, threshold: float) -> list[tuple[str, float]]:
        return [(k, v) for k, v in sorted(self.label_scores.items(), key=lambda kv: -kv[1]) if v >= threshold]


class ZeroShot(Protocol):
    def classify(self, cap: AwarenessCaptureView) -> ZeroShotResult: ...


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'+\-]+")


class ZeroShotStub:
    """Deterministic alias-overlap classifier. Production-safe default."""

    model_version = "stub-zero-shot/v1"

    def __init__(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy
        # Build inverted index: word/alias → set(label ids).
        #
        # BUG-EE fix: previously this also indexed every >3-char non-stop
        # word from `d.hypothesis` (e.g. "This text is about a central bank
        # decision, rate move, or monetary policy."). That dumped generic
        # English ("move", "decision", "report", "event", "major", "impact")
        # into the index, all pointing at whatever label whose hypothesis
        # they came from. Result: a BTC headline saying "Analysts attribute
        # the **move** to institutional demand" fired `central_bank` —
        # because "move" was a central_bank hypothesis token. The stub
        # should rely on the curated `id` + `aliases` set only; the
        # full-hypothesis path is meant for the BART-MNLI model template,
        # not for keyword-overlap.
        self._index: dict[str, set[str]] = {}
        for group in (taxonomy.asset_classes, taxonomy.impact_reason_codes, taxonomy.negative_class):
            for d in group:
                tokens = {d.id.replace("_", " ").lower(), *[a.lower() for a in d.aliases]}
                for t in tokens:
                    self._index.setdefault(t, set()).add(d.id)

    # Title weight relative to body. Pre-fix this multiplier was applied by
    # repeating the title 3x in a concatenated string and then calling
    # `set(...)`, which destroyed every repetition — so the documented
    # "title carries 3x weight" was a no-op. Bigrams (which used a list,
    # not a set) DID get the 3x boost, an undocumented asymmetry that
    # silently inflated multi-word-alias scores over single-word ones.
    _TITLE_WEIGHT = 3.0
    _BIGRAM_WEIGHT = 1.5

    def classify(self, cap: AwarenessCaptureView) -> ZeroShotResult:
        title_l = (cap.title or "").lower()
        body_l = (cap.text or "")[:3000].lower()

        # Multi-set frequency-based scoring via Counter: repeated mentions
        # of a label-aliased token increase the score. Title-overlap is
        # completely excluded from the body count so body mentions of a word
        # in the title do not double-count (title weight already covers it).
        from collections import Counter

        title_words = Counter(
            w for w in _WORD_RE.findall(title_l)
            if w not in _STOP
        )
        body_words = Counter(
            w for w in _WORD_RE.findall(body_l)
            if w not in _STOP
        )
        # Exclude title unigram overlap from body
        for w in title_words:
            if w in body_words:
                del body_words[w]

        title_bigrams = Counter(_bigrams(title_l))
        body_bigrams = Counter(_bigrams(body_l))
        # Exclude title bigram overlap from body
        for b in title_bigrams:
            if b in body_bigrams:
                del body_bigrams[b]

        scores: dict[str, float] = {}

        def _add(token: str, weight: float) -> None:
            for label_id in self._index.get(token, ()):
                scores[label_id] = scores.get(label_id, 0.0) + weight

        # Unigrams — title tokens carry _TITLE_WEIGHT, body tokens carry 1.0.
        for w, count in title_words.items():
            _add(w, self._TITLE_WEIGHT * count)
        for w, count in body_words.items():
            _add(w, 1.0 * count)
        # Bigrams (multi-word aliases) — same title boost; base weight 1.5.
        for b, count in title_bigrams.items():
            _add(b, self._BIGRAM_WEIGHT * self._TITLE_WEIGHT * count)
        for b, count in body_bigrams.items():
            _add(b, self._BIGRAM_WEIGHT * count)

        if not scores:
            return ZeroShotResult(label_scores={}, model_version=self.model_version)

        # Sigmoid-style normalization that keeps values in (0, 1).
        normalized: dict[str, float] = {}
        for k, raw in scores.items():
            normalized[k] = float(1.0 / (1.0 + math.exp(-(raw - 1.0))))
        return ZeroShotResult(label_scores=normalized, model_version=self.model_version)


class ZeroShotModel:
    """Wraps transformers pipeline("zero-shot-classification"). Lazy import."""

    def __init__(self, taxonomy: Taxonomy, model_name: str = "facebook/bart-large-mnli") -> None:
        from transformers import pipeline  # type: ignore[import-not-found]

        self.taxonomy = taxonomy
        self.model_name = model_name
        self._pipe = pipeline("zero-shot-classification", model=model_name, device=-1)
        self._candidate_hypotheses = list(taxonomy.all_hypotheses().items())  # (id, hypothesis)

    @property
    def model_version(self) -> str:
        return f"hf:{self.model_name}"

    def classify(self, cap: AwarenessCaptureView) -> ZeroShotResult:
        text = (cap.title or "") + "\n" + (cap.text or "")[:1500]
        if not text.strip():
            return ZeroShotResult(label_scores={}, model_version=self.model_version)
        # Use hypothesis-style template for multi-label.
        hyps = [h for _, h in self._candidate_hypotheses]
        out = self._pipe(text, candidate_labels=hyps, multi_label=True)
        # HF zero-shot pipeline contract: out["labels"] and out["scores"]
        # are guaranteed same length; strict=True surfaces a contract break.
        scores_by_hyp = {
            lbl: float(s) for lbl, s in zip(out["labels"], out["scores"], strict=True)
        }
        scores: dict[str, float] = {}
        for lid, hyp in self._candidate_hypotheses:
            scores[lid] = scores_by_hyp.get(hyp, 0.0)
        return ZeroShotResult(label_scores=scores, model_version=self.model_version)


def make_zero_shot(taxonomy: Taxonomy, model_name: str, use_stub: bool) -> ZeroShot:
    if use_stub:
        return ZeroShotStub(taxonomy)
    try:
        return ZeroShotModel(taxonomy, model_name=model_name)
    except Exception:
        return ZeroShotStub(taxonomy)


_STOP = frozenset(
    {
        "this", "that", "the", "and", "for", "with", "about", "into", "from", "over",
        "under", "between", "among", "what", "which", "who", "whom", "are", "was",
        "were", "been", "being", "have", "has", "had", "they", "them", "their",
        "there", "here", "when", "where", "while", "though", "than", "then", "such",
        "some", "many", "much", "most", "more", "less", "few", "any", "all",
        "primarily", "text", "report", "covers",
    }
)


def _bigrams(text: str) -> list[str]:
    tokens = [t.lower() for t in _WORD_RE.findall(text) if t.lower() not in _STOP]
    # itertools.pairwise yields (t0,t1), (t1,t2), ... so the last token has
    # no pair (intent: bigrams over consecutive token pairs).
    return [f"{a} {b}" for a, b in pairwise(tokens)]
