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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

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

    scope: str  # "overall" / "asset_class:equities" / "symbol:BTC-USD"
    sample_size: int
    counts: dict  # {"positive": n1, "neutral": n2, "negative": n3}
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


def _compute_dispersion_from_counts(
    pos_count: int,
    neu_count: int,
    neg_count: int,
    scope: str,
) -> DispersionResult:
    total = pos_count + neu_count + neg_count
    if total == 0:
        return DispersionResult(
            scope=scope,
            sample_size=0,
            counts={"positive": 0, "neutral": 0, "negative": 0},
            entropy=0.0,
            max_entropy=_MAX_ENTROPY,
            normalized_entropy=0.0,
            dominant_label="tied",
        )

    # Calculate entropy
    entropy = 0.0
    if pos_count > 0:
        p = pos_count / total
        entropy -= p * math.log2(p)
    if neu_count > 0:
        p = neu_count / total
        entropy -= p * math.log2(p)
    if neg_count > 0:
        p = neg_count / total
        entropy -= p * math.log2(p)

    normalized = entropy / _MAX_ENTROPY

    # Find dominant label
    if pos_count > neu_count:
        if pos_count > neg_count:
            dominant = "positive"
        elif pos_count < neg_count:
            dominant = "negative"
        else:
            dominant = "tied"
    elif pos_count < neu_count:
        if neu_count > neg_count:
            dominant = "neutral"
        elif neu_count < neg_count:
            dominant = "negative"
        else:
            dominant = "tied"
    else:
        if pos_count > neg_count:
            dominant = "tied"
        elif pos_count < neg_count:
            dominant = "negative"
        else:
            dominant = "tied"

    return DispersionResult(
        scope=scope,
        sample_size=total,
        counts={"positive": pos_count, "neutral": neu_count, "negative": neg_count},
        entropy=entropy,
        max_entropy=_MAX_ENTROPY,
        normalized_entropy=normalized,
        dominant_label=dominant,
    )


def compute_dispersion(sentiments: Iterable[str | None]) -> DispersionResult:
    """Compute Shannon entropy for a list of sentiment labels.

    Anything that is not one of the canonical pos/neu/neg strings is
    silently skipped — including None, "mixed", "", or non-string junk.
    The empty-input path returns a zero result with the canonical
    counts dict so the UI can render a uniform shape.
    """

    pos_count = 0
    neu_count = 0
    neg_count = 0
    for s in sentiments:
        if s == "positive":
            pos_count += 1
        elif s == "neutral":
            neu_count += 1
        elif s == "negative":
            neg_count += 1

    total = pos_count + neu_count + neg_count
    if total == 0:
        return DispersionResult(
            scope="empty",
            sample_size=0,
            counts={"positive": 0, "neutral": 0, "negative": 0},
            entropy=0.0,
            max_entropy=_MAX_ENTROPY,
            normalized_entropy=0.0,
            dominant_label="tied",
        )
    return _compute_dispersion_from_counts(pos_count, neu_count, neg_count, "overall")


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

    buckets: dict[str, list[int]] = {}
    scope_label = _SCOPE_LABELS.get(scope_key, scope_key)

    for r in records:
        if not isinstance(r, dict):
            continue
        sentiment = r.get("sentiment_label")
        scope_value = r.get(scope_key)
        if scope_value is None:
            continue

        if isinstance(scope_value, list):
            iterable = scope_value
        else:
            iterable = (scope_value,)

        is_pos = sentiment == "positive"
        is_neu = sentiment == "neutral"
        is_neg = sentiment == "negative"

        for s in iterable:
            if not s or not isinstance(s, str):
                continue
            counts = buckets.get(s)
            if counts is None:
                counts = [0, 0, 0, 0]
                buckets[s] = counts
            counts[3] += 1
            if is_pos:
                counts[0] += 1
            elif is_neu:
                counts[1] += 1
            elif is_neg:
                counts[2] += 1

    results: list[DispersionResult] = []
    # Sort by sample size DESC then bucket name ASC for determinism.
    for bucket_name, counts in sorted(buckets.items(), key=lambda b: (-b[1][3], b[0])):
        results.append(
            _compute_dispersion_from_counts(
                counts[0],
                counts[1],
                counts[2],
                f"{scope_label}:{bucket_name}",
            )
        )
    return results
