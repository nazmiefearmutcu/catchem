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
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from .schemas import AwarenessCaptureView
from .taxonomy import LabelDef, Taxonomy


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
        # Build inverted index: word/alias → set(label ids)
        self._index: dict[str, set[str]] = {}
        for group in (taxonomy.asset_classes, taxonomy.impact_reason_codes, taxonomy.negative_class):
            for d in group:
                tokens = {d.id.replace("_", " ").lower(), *[a.lower() for a in d.aliases]}
                # Hypothesis-derived keywords too
                tokens |= {w.lower() for w in _WORD_RE.findall(d.hypothesis) if len(w) > 3 and w.lower() not in _STOP}
                for t in tokens:
                    self._index.setdefault(t, set()).add(d.id)

    def classify(self, cap: AwarenessCaptureView) -> ZeroShotResult:
        title_l = (cap.title or "").lower()
        body_l = (cap.text or "").lower()
        # Title carries 3× weight relative to body.
        weighted = f"{(title_l + ' ') * 3}{body_l[:3000]}"
        words = set(w.lower() for w in _WORD_RE.findall(weighted) if w.lower() not in _STOP)

        scores: dict[str, float] = {}
        for token in words:
            for label_id in self._index.get(token, ()):
                scores[label_id] = scores.get(label_id, 0.0) + 1.0
        # Add bigram hits for multi-word aliases.
        for bigram in _bigrams(weighted):
            for label_id in self._index.get(bigram, ()):
                scores[label_id] = scores.get(label_id, 0.0) + 1.5

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
        labels_id = [lid for lid, _ in self._candidate_hypotheses]
        hyps = [h for _, h in self._candidate_hypotheses]
        out = self._pipe(text, candidate_labels=hyps, multi_label=True)
        scores_by_hyp = {lbl: float(s) for lbl, s in zip(out["labels"], out["scores"])}
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
    return [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
