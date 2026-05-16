"""Stage A: fast prefilter. Cheap rule-based decisions on title/domain/source.

We are **conservative**: only reject items that look obviously non-finance.
Borderline items move on to Stage B (zero-shot) and the scoring layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .schemas import AwarenessCaptureView
from .taxonomy import Taxonomy


_FINANCE_KEYWORDS = (
    "earnings", "revenue", "profit", "loss", "guidance", "ipo", "merger",
    "acquisition", "buyback", "dividend", "stock", "share", "equity", "bond",
    "yield", "treasury", "rate", "rates", "fed", "ecb", "boj", "boe", "central bank",
    "inflation", "cpi", "ppi", "gdp", "unemployment", "jobs", "payrolls",
    "oil", "brent", "wti", "gold", "silver", "copper", "wheat",
    "bitcoin", "btc", "ether", "ethereum", "crypto", "stablecoin",
    "forex", "currency", "dollar", "euro", "yen", "yuan", "pound",
    "sanctions", "tariff", "trade war", "geopolit", "regulation", "lawsuit",
    "sec ", "fdic", "occ ", "fca ",
    "$",  # cashtag indicator
)

_HARD_NEGATIVE_KEYWORDS = (
    "scoreboard", "touchdown", "goal", "transfer window", "match", "draft pick",
    "celebrity gossip", "movie review", "film review", "concert review",
    "recipe", "horoscope", "tarot", "love advice",
)


@dataclass
class PrefilterResult:
    keep: bool
    rule_score: float           # 0..1, prior confidence that this is finance
    matched_keywords: tuple[str, ...]
    blocked_keywords: tuple[str, ...]
    domain_prior: float
    source_type_prior: float


class FastPrefilter:
    """Conservative gate. Default behavior: keep unless clearly irrelevant.

    The rule_score is exposed as a `prefilter_prior` component score downstream.
    """

    def __init__(self, taxonomy: Taxonomy, min_text_chars: int = 80) -> None:
        self.taxonomy = taxonomy
        self.min_text_chars = int(min_text_chars)

    @staticmethod
    def _excerpt(cap: AwarenessCaptureView, max_chars: int = 800) -> str:
        title = (cap.title or "").strip()
        body = (cap.text or "").strip()[:max_chars]
        return f"{title}\n{body}".lower()

    @staticmethod
    def _word_match(text: str, keyword: str) -> bool:
        """Whole-word match for alphanumeric keywords; substring for the rest."""
        k = keyword.strip().lower()
        if not k:
            return False
        # "$" / multi-word phrases / punctuation-bearing keywords: substring.
        if not k.replace(" ", "").isalnum():
            return k in text
        # Word boundaries on each end. Multi-word keywords still work because
        # whitespace is alnum-adjacent.
        return re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text) is not None

    def evaluate(self, cap: AwarenessCaptureView) -> PrefilterResult:
        text = self._excerpt(cap)
        domain_prior = self.taxonomy.domain_prior(cap.domain)
        source_prior = self.taxonomy.source_type_prior(cap.source_type)

        matched = tuple(k for k in _FINANCE_KEYWORDS if self._word_match(text, k))
        blocked = tuple(k for k in _HARD_NEGATIVE_KEYWORDS if self._word_match(text, k))

        # Cashtag detection ($AAPL, $BTC). Independent signal.
        if re.search(r"\$[A-Z]{1,6}\b", cap.text or ""):
            matched = matched + ("cashtag",)

        # Score: bounded combination of priors + keyword density + cashtags.
        if len(text) < self.min_text_chars:
            rule_score = max(domain_prior, 0.20)
        else:
            keyword_signal = min(1.0, len(matched) / 4.0)
            rule_score = max(0.0, min(1.0, 0.45 * domain_prior + 0.20 * source_prior + 0.35 * keyword_signal))

        keep = True
        # Two-rule veto: many negative hits AND no finance hits AND low domain prior → drop.
        if len(blocked) >= 2 and not matched and domain_prior < 0.30:
            keep = False
            rule_score = 0.05
        return PrefilterResult(
            keep=keep,
            rule_score=rule_score,
            matched_keywords=matched,
            blocked_keywords=blocked,
            domain_prior=domain_prior,
            source_type_prior=source_prior,
        )
