"""
Sentiment dispersion signal — Shannon entropy over pos/neg/neutral
sentiment distribution.

H(S) = -Σ p(s) * log2(p(s))

Max entropy: log2(3) ≈ 1.585 when {pos: 1/3, neutral: 1/3, neg: 1/3}
Min entropy: 0 when one sentiment dominates 100%

High dispersion (≥0.95) indicates analyst-disagreement on direction —
often a sign of regime uncertainty, mixed actors (some bulls some bears).

Low dispersion (≤0.3) indicates one-sided narrative — sentiment is
"all aligned", which can precede momentum continuation OR mean-reversion.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DispersionResult",
    "compute_by_scope",
    "compute_dispersion",
]


_LABELS: tuple[str, str, str] = ("positive", "neutral", "negative")
_MAX_ENTROPY: float = math.log2(3)

# Pretty singular labels for the scope prefix in compute_by_scope. The keys
# are the catchem record field names; the values are short, human-readable
# tags the UI can show as "asset_class:equities" instead of "asset_classe:".
_SCOPE_LABELS: dict[str, str] = {
    "asset_classes": "asset_class",
    "candidate_symbols": "symbol",
    "reasons": "reason",
    "actor_types": "actor",
}


@dataclass(frozen=True)
class DispersionResult:
    """Per-scope Shannon-entropy summary.

    ``normalized_entropy`` ∈ [0, 1] makes the cross-scope comparison
    cheap: 0 = unanimous, 1 = perfect three-way split. The raw
    ``entropy`` is kept for callers that want the bits-of-information
    interpretation directly.
    """

    scope: str        # "overall" / "asset_class:equities" / "symbol:BTC-USD"
    sample_size: int
    counts: dict      # {"positive": n1, "neutral": n2, "negative": n3}
    entropy: float
    max_entropy: float  # always log2(3) ≈ 1.585
    normalized_entropy: float  # entropy / max_entropy ∈ [0, 1]
    dominant_label: str  # "positive" / "neutral" / "negative" / "tied"


def _dominant(counts: Counter) -> str:
    """Return the single most-common label, or "tied" if the top two match.

    Counter.most_common does not break ties deterministically, so we have
    to inspect the top two explicitly. Empty counters fall through to
    "tied" which mirrors the empty-input shape from ``compute_dispersion``.
    """

    if not counts:
        return "tied"
    top = counts.most_common(2)
    if len(top) >= 2 and top[0][1] == top[1][1]:
        return "tied"
    return top[0][0]


def compute_dispersion(sentiments: Iterable[str | None]) -> DispersionResult:
    """Compute Shannon entropy for a list of sentiment labels.

    Anything that is not one of the canonical pos/neu/neg strings is
    silently skipped — including None, "mixed", "", or non-string junk.
    The empty-input path returns a zero result with the canonical
    counts dict so the UI can render a uniform shape.
    """

    valid = [s for s in sentiments if s in _LABELS]
    if not valid:
        return DispersionResult(
            scope="empty",
            sample_size=0,
            counts={k: 0 for k in _LABELS},
            entropy=0.0,
            max_entropy=_MAX_ENTROPY,
            normalized_entropy=0.0,
            dominant_label="tied",
        )

    counts = Counter(valid)
    total = sum(counts.values())
    probs = [counts[k] / total for k in _LABELS]

    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    normalized = entropy / _MAX_ENTROPY if _MAX_ENTROPY > 0 else 0.0

    return DispersionResult(
        scope="overall",
        sample_size=total,
        counts={k: counts.get(k, 0) for k in _LABELS},
        entropy=entropy,
        max_entropy=_MAX_ENTROPY,
        normalized_entropy=normalized,
        dominant_label=_dominant(counts),
    )


def compute_by_scope(
    records: list[dict],
    scope_key: str = "asset_classes",
) -> list[DispersionResult]:
    """Bucket records by a scope key and compute dispersion per bucket.

    ``scope_key`` is read off each record; the value may be a scalar or
    a list (e.g. ``asset_classes: ["equities", "crypto"]``). For list
    values, a record contributes one sentiment-vote to every listed
    bucket so a multi-class story lifts all relevant buckets at once.

    Falsy bucket values (None, "", []) are skipped — they would all
    collapse into a single fake bucket otherwise. The result is sorted
    by sample size DESC so heavily-covered scopes float to the top.
    """

    if not records:
        return []

    buckets: dict[str, list[str | None]] = defaultdict(list)
    # Use the curated singular label when we have one; fall back to the
    # raw key so callers passing unknown fields still get a sane prefix
    # rather than a hard error.
    scope_label = _SCOPE_LABELS.get(scope_key, scope_key)

    for r in records or []:
        if not isinstance(r, dict):
            continue
        sentiment = r.get("sentiment_label")
        scope_value = r.get(scope_key) or []
        if isinstance(scope_value, list):
            iterable: Iterable[Any] = scope_value
        else:
            iterable = [scope_value]
        for s in iterable:
            if not s or not isinstance(s, str):
                continue
            buckets[s].append(sentiment)

    results: list[DispersionResult] = []
    # Sort by sample size DESC then bucket name ASC for determinism.
    for bucket_name, sents in sorted(
        buckets.items(), key=lambda b: (-len(b[1]), b[0])
    ):
        r = compute_dispersion(sents)
        # Replace the "overall" scope with the actual bucket identifier.
        results.append(
            DispersionResult(
                scope=f"{scope_label}:{bucket_name}",
                sample_size=r.sample_size,
                counts=r.counts,
                entropy=r.entropy,
                max_entropy=r.max_entropy,
                normalized_entropy=r.normalized_entropy,
                dominant_label=r.dominant_label,
            )
        )
    return results
