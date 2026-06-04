"""Extractive evidence: pick the 1-N sentences that best support the labels.

No generation. Pure ranking over sentences in title+body using keyword overlap
against the chosen labels (zero-shot top hits + entity hits).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence

from .schemas import AwarenessCaptureView

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?])\s+(?=[A-Z\$0-9])")
_BOILERPLATE_RE = re.compile(
    r"\b("
    r"follow us|subscribe|sign up|newsletter|cookie|cookies|privacy policy|"
    r"terms of service|advertisement|sponsored|all rights reserved|"
    r"share this|click here|download our app"
    r")\b",
    re.IGNORECASE,
)


def is_boilerplate_sentence(text: str) -> bool:
    return bool(_BOILERPLATE_RE.search(text or ""))


def clean_boilerplate_text(text: str) -> str:
    """Drop common publisher footer/CTA sentences before entity/evidence use."""
    kept = [s for s in split_sentences(text) if not is_boilerplate_sentence(s)]
    return " ".join(kept)


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    # split into sentences, then dedupe and trim
    parts = _SENTENCE_SPLIT_RE.split(text)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        s = p.strip()
        if not s:
            continue
        if is_boilerplate_sentence(s):
            continue
        if len(s) > 400:
            s = s[:400].rsplit(" ", 1)[0] + "…"
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


@functools.lru_cache(maxsize=1024)
def _get_term_pattern(term_lc: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term_lc)}(?![a-z0-9])")


def _sentence_word_match(sentence_lc: str, term_lc: str) -> bool:
    """Word-boundary match — pre-fix `term in sentence_lc` was substring,
    so the term "rate" matched "operating" and "fed" matched "federated".
    Multi-word terms ("central bank") still work because the regex escape
    keeps internal whitespace literal and \\b anchors on the outer chars.
    """
    if not term_lc:
        return False
    return _get_term_pattern(term_lc).search(sentence_lc) is not None


def extract_evidence(
    cap: AwarenessCaptureView,
    label_terms: Sequence[str],
    entity_terms: Sequence[str],
    top_k: int = 3,
) -> list[str]:
    """Score sentences by term overlap; return the best ``top_k``."""
    title = (cap.title or "").strip()
    body = cap.text or ""
    sentences: list[str] = []
    if title:
        sentences.append(title)
    sentences.extend(split_sentences(body))
    if not sentences:
        return []
    terms = {t.lower() for t in (*label_terms, *entity_terms) if t}
    if not terms:
        # No labels yet — return the first sentence as a sane fallback.
        return [sentences[0]]

    # Pre-compile the terms for this function call to minimize lookup/compilation overhead
    compiled_patterns = [_get_term_pattern(t) for t in terms]

    scored: list[tuple[float, int, str]] = []
    for idx, s in enumerate(sentences):
        s_lc = s.lower()
        score = sum(1.0 for pattern in compiled_patterns if pattern.search(s_lc) is not None)
        if idx == 0:
            score += 0.5  # title boost
        scored.append((score, -idx, s))  # later sentences lose ties
    scored.sort(reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for score, _, s in scored:
        if score <= 0:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= top_k:
            break
    return out


def build_reason_text(
    asset_classes: Iterable[str], reason_codes: Iterable[str], sentiment_label: str | None
) -> str:
    asset_part = "/".join(asset_classes) or "general"
    reason_part = "/".join(reason_codes) or "no-specific-reason"
    sent_part = f"sentiment={sentiment_label}" if sentiment_label else "sentiment=unknown"
    return f"{asset_part} | {reason_part} | {sent_part}"
