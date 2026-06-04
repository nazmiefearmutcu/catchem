"""Novelty detection for FinancialImpactRecord rows.

Scores how genuinely NEW a record is relative to a recent corpus.

Pipeline
--------
1. Build a token-set feature per record from lowercase title + first 500
   chars of text_excerpt (alnum tokenize, drop short tokens + tiny inline
   stopword list).
2. Similarity between records is a weighted blend of four Jaccards:
   * 0.55 tokens
   * 0.20 candidate_symbols
   * 0.15 impact_reason_codes
   * 0.10 asset_classes
3. novelty_score = 1 - max similarity to any other corpus row.

Performance
-----------
Pairwise comparison is O(N^2) in corpus size. Intended for recent windows
of up to ~500 records; if you need more, layer in a coarse symbol/domain
prefilter before calling `score_corpus`. The token set is computed once
per record and cached inside `score_corpus`, so the constant factor is
small.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "to",
        "for",
        "is",
        "are",
        "was",
        "were",
        "and",
        "or",
        "but",
        "with",
        "from",
        "this",
        "that",
        "by",
        "as",
    }
)

# Weights for the four-way Jaccard blend. Sum is 1.0 by construction so
# the resulting similarity stays in [0, 1].
_W_TOKENS = 0.55
_W_SYMBOLS = 0.20
_W_REASONS = 0.15
_W_CLASSES = 0.10

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_EXCERPT_CHARS = 500
_MIN_TOKEN_LEN = 3


@dataclass(frozen=True)
class NoveltyResult:
    """Per-record novelty verdict.

    `novelty_score` and `max_similarity_to_corpus` always sum to 1.0.
    `nearest_*` fields are None when the comparison corpus is empty
    (post-self-exclusion).
    """

    capture_id: str
    novelty_score: float
    max_similarity_to_corpus: float
    nearest_capture_id: str | None
    nearest_title: str | None
    matched_symbols: tuple[str, ...]
    explanation: str


def _tokenize(title: str | None, text_excerpt: str | None) -> frozenset[str]:
    """Lowercase + alnum-tokenize title + first 500 chars of excerpt."""

    parts: list[str] = []
    if title:
        parts.append(title)
    if text_excerpt:
        parts.append(text_excerpt[:_EXCERPT_CHARS])
    if not parts:
        return frozenset()
    blob = " ".join(parts).lower()
    raw = _TOKEN_SPLIT_RE.split(blob)
    return frozenset(tok for tok in raw if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS)


def _to_string_set(values: object) -> frozenset[str]:
    """Coerce an arbitrary list-ish field into a frozenset of lower-stripped strings."""

    if not values:
        return frozenset()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    out: list[str] = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip().lower()
        if s:
            out.append(s)
    return frozenset(out)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    a_len = len(a)
    b_len = len(b)
    inter = len(a & b)
    union = a_len + b_len - inter
    if union == 0:
        return 0.0
    return inter / union


@dataclass(frozen=True)
class _Features:
    """Cached per-record feature bundle used by similarity calculations."""

    capture_id: str
    title: str | None
    tokens: frozenset[str]
    symbols: frozenset[str]
    reasons: frozenset[str]
    classes: frozenset[str]
    tokens_len: int
    symbols_len: int
    reasons_len: int
    classes_len: int


def _features(record: dict) -> _Features:
    tokens = _tokenize(record.get("title"), record.get("text_excerpt"))
    symbols = _to_string_set(record.get("candidate_symbols"))
    reasons = _to_string_set(record.get("impact_reason_codes"))
    classes = _to_string_set(record.get("asset_classes"))
    return _Features(
        capture_id=str(record.get("capture_id") or ""),
        title=record.get("title"),
        tokens=tokens,
        symbols=symbols,
        reasons=reasons,
        classes=classes,
        tokens_len=len(tokens),
        symbols_len=len(symbols),
        reasons_len=len(reasons),
        classes_len=len(classes),
    )


def _similarity(a: _Features, b: _Features) -> float:
    t_inter = len(a.tokens & b.tokens)
    t_union = a.tokens_len + b.tokens_len - t_inter
    t_jac = t_inter / t_union if t_union > 0 else 0.0

    s_inter = len(a.symbols & b.symbols)
    s_union = a.symbols_len + b.symbols_len - s_inter
    s_jac = s_inter / s_union if s_union > 0 else 0.0

    r_inter = len(a.reasons & b.reasons)
    r_union = a.reasons_len + b.reasons_len - r_inter
    r_jac = r_inter / r_union if r_union > 0 else 0.0

    c_inter = len(a.classes & b.classes)
    c_union = a.classes_len + b.classes_len - c_inter
    c_jac = c_inter / c_union if c_union > 0 else 0.0

    return _W_TOKENS * t_jac + _W_SYMBOLS * s_jac + _W_REASONS * r_jac + _W_CLASSES * c_jac


def _explain(max_sim: float, nearest_title: str | None, corpus_empty: bool) -> str:
    """Stable, human-readable similarity bucket."""

    title_label = nearest_title.strip() if nearest_title else "untitled record"
    if corpus_empty or max_sim < 0.10:
        return "first of kind in corpus"
    if max_sim >= 0.85:
        return f"near-duplicate of {title_label}"
    if max_sim >= 0.50:
        return f"shares symbols + theme with {title_label}"
    return "low overlap with prior coverage"


def _result_from(
    target: _Features,
    others: list[_Features],
) -> NoveltyResult:
    """Build a NoveltyResult by scanning `others` (target already excluded)."""

    if not others:
        return NoveltyResult(
            capture_id=target.capture_id,
            novelty_score=1.0,
            max_similarity_to_corpus=0.0,
            nearest_capture_id=None,
            nearest_title=None,
            matched_symbols=(),
            explanation=_explain(0.0, None, corpus_empty=True),
        )

    best_sim = -1.0
    best: _Features | None = None
    for cand in others:
        sim = _similarity(target, cand)
        if sim > best_sim:
            best_sim = sim
            best = cand

    # `best` is guaranteed non-None because `others` is non-empty above.
    assert best is not None
    matched = tuple(sorted(target.symbols & best.symbols))
    return NoveltyResult(
        capture_id=target.capture_id,
        novelty_score=1.0 - best_sim,
        max_similarity_to_corpus=best_sim,
        nearest_capture_id=best.capture_id or None,
        nearest_title=best.title,
        matched_symbols=matched,
        explanation=_explain(best_sim, best.title, corpus_empty=False),
    )


def compute_novelty(record: dict, corpus: list[dict]) -> NoveltyResult:
    """Score one record against a corpus.

    The record's own `capture_id` is excluded from `corpus` if present, so
    callers can pass the full window without having to filter first.
    """

    target = _features(record)
    # Exclude exactly ONE self-match, not every row whose capture_id equals
    # the target's. Excluding by value collapses for empty/missing ids: an
    # empty target id would str()-equal every other id-less row and wrongly
    # drop genuine near-duplicates, reporting them as fully novel. Mirror
    # score_corpus's index-based single exclusion instead: skip the first row
    # that IS the target (by object identity, falling back to a non-empty
    # capture_id match) and keep the rest.
    others: list[_Features] = []
    self_dropped = False
    for r in corpus:
        if not self_dropped and (
            r is record or (target.capture_id and str(r.get("capture_id") or "") == target.capture_id)
        ):
            self_dropped = True
            continue
        others.append(_features(r))
    return _result_from(target, others)


def score_corpus(corpus: list[dict]) -> list[NoveltyResult]:
    """Score every record in `corpus` against the rest of the corpus.

    Returns results in the same order as the input. O(N^2) — see module
    docstring for the perf budget.
    """

    feats = [_features(r) for r in corpus]
    n = len(feats)
    results: list[NoveltyResult] = []
    for i in range(n):
        target = feats[i]
        best_sim = -1.0
        best: _Features | None = None
        for j in range(n):
            if j == i:
                continue
            cand = feats[j]
            sim = _similarity(target, cand)
            if sim > best_sim:
                best_sim = sim
                best = cand

        if best is None:
            results.append(
                NoveltyResult(
                    capture_id=target.capture_id,
                    novelty_score=1.0,
                    max_similarity_to_corpus=0.0,
                    nearest_capture_id=None,
                    nearest_title=None,
                    matched_symbols=(),
                    explanation=_explain(0.0, None, corpus_empty=True),
                )
            )
        else:
            matched = tuple(sorted(target.symbols & best.symbols))
            results.append(
                NoveltyResult(
                    capture_id=target.capture_id,
                    novelty_score=1.0 - best_sim,
                    max_similarity_to_corpus=best_sim,
                    nearest_capture_id=best.capture_id or None,
                    nearest_title=best.title,
                    matched_symbols=matched,
                    explanation=_explain(best_sim, best.title, corpus_empty=False),
                )
            )
    return results


__all__ = [
    "NoveltyResult",
    "compute_novelty",
    "score_corpus",
]
