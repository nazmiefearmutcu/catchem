"""Stage E: re-ranking. We only use this when there are *multiple* candidate
matches to disambiguate (e.g. a headline mentions "Tesla" and we have several
candidate tickers/aliases). For one-shot ranking we stick with rapidfuzz.

Both implementations expose the same surface: ``rank(query, candidates) -> list[(candidate, score)]``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from rapidfuzz import fuzz


class Reranker(Protocol):
    @property
    def model_version(self) -> str: ...
    def rank(self, query: str, candidates: Iterable[str]) -> list[tuple[str, float]]: ...


class RerankerStub:
    """rapidfuzz token-set-ratio as a deterministic CPU reranker."""

    model_version = "stub-rerank/v1"

    def rank(self, query: str, candidates: Iterable[str]) -> list[tuple[str, float]]:
        q = (query or "").lower()
        scored = [(c, float(fuzz.token_set_ratio(q, str(c).lower())) / 100.0) for c in candidates]
        scored.sort(key=lambda kv: -kv[1])
        return scored


class RerankerModel:
    """Cross-encoder reranker (lazy import)."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L6-v2") -> None:
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

        self.model_name = model_name
        self._model = CrossEncoder(model_name, device="cpu")

    @property
    def model_version(self) -> str:
        return f"hf:{self.model_name}"

    def rank(self, query: str, candidates: Iterable[str]) -> list[tuple[str, float]]:
        cands = list(candidates)
        if not cands:
            return []
        pairs = [(query, c) for c in cands]
        scores = self._model.predict(pairs)
        # strict=True — CrossEncoder.predict returns exactly len(pairs) scores.
        out = [(c, float(s)) for c, s in zip(cands, scores, strict=True)]
        out.sort(key=lambda kv: -kv[1])
        return out


def make_reranker(model_name: str, use_stub: bool) -> Reranker:
    if use_stub:
        return RerankerStub()
    try:
        return RerankerModel(model_name=model_name)
    except Exception:
        return RerankerStub()
