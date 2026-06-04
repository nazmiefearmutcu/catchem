"""Stage F (part 1): deterministic entity extraction.

We avoid heavy NER models. The signals we need are:
  * cashtags ($AAPL, $BTC)
  * ALL-CAPS tickers in a small set of known patterns (AAPL, MSFT, XOM)
  * known company aliases (loaded from registry)
  * known indices, currencies, central banks
  * proper-noun runs in the title

The output is `EntityHits` — a structured list of candidate entities with their
detection sources. Symbol/channel mapping consume these hits next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from .symbol_mapper import _TICKER_DENYLIST

_CASHTAG_RE = re.compile(r"\$([A-Z]{1,6})\b")
_TICKER_PAREN_RE = re.compile(r"\(([A-Z]{2,6})\)")
# Capitalized phrase runs up to 4 words
_PROPER_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,3})\b")


_KNOWN_CURRENCIES = {
    "USD",
    "EUR",
    "JPY",
    "GBP",
    "CHF",
    "CAD",
    "AUD",
    "NZD",
    "CNY",
    "TRY",
    "INR",
    "MXN",
    "BRL",
    "ZAR",
    "KRW",
    "SEK",
    "NOK",
    "PLN",
    "RUB",
}
_KNOWN_CENTRAL_BANKS = {
    "Federal Reserve",
    "Fed",
    "FOMC",
    "Federal Open Market Committee",
    "ECB",
    "European Central Bank",
    "Bank of Japan",
    "BoJ",
    "Bank of England",
    "BoE",
    "People's Bank of China",
    "PBoC",
    "Bank of Canada",
    "BoC",
    "Reserve Bank of Australia",
    "RBA",
    "Reserve Bank of New Zealand",
    "RBNZ",
    "Swiss National Bank",
    "SNB",
    "Bank of Korea",
    "BoK",
    "Central Bank of Turkey",
}
_KNOWN_INDICES = {
    "S&P 500",
    "S&P500",
    "S&P",
    "Dow",
    "Dow Jones",
    "Nasdaq",
    "Russell 2000",
    "FTSE",
    "FTSE 100",
    "DAX",
    "CAC",
    "CAC 40",
    "Nikkei",
    "Nikkei 225",
    "Hang Seng",
    "Shanghai Composite",
    "STOXX 600",
    "Euro Stoxx",
    "BIST",
    "BIST 100",
    "VIX",
}
_KNOWN_COMMODITIES = {
    "Brent",
    "WTI",
    "crude",
    "natural gas",
    "gold",
    "silver",
    "copper",
    "platinum",
    "palladium",
    "wheat",
    "corn",
    "soybeans",
    "coffee",
    "sugar",
    "cocoa",
}
_KNOWN_CRYPTO = {
    "Bitcoin",
    "BTC",
    "Ethereum",
    "ETH",
    "Solana",
    "SOL",
    "Cardano",
    "ADA",
    "Ripple",
    "XRP",
    "Dogecoin",
    "DOGE",
    "Polkadot",
    "DOT",
    "stablecoin",
}


@lru_cache(maxsize=512)
def _compile_alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)


def _contains_alias(text: str, alias: str) -> bool:
    if len(alias.strip()) <= 1:
        return False
    return _compile_alias_pattern(alias).search(text) is not None


_CURRENCY_PATTERNS = {ccy: re.compile(rf"\b{ccy}\b") for ccy in _KNOWN_CURRENCIES}
_CENTRAL_BANK_PATTERNS = {cb: re.compile(rf"\b{re.escape(cb)}\b") for cb in _KNOWN_CENTRAL_BANKS}
_INDEX_PATTERNS = {idx: re.compile(rf"\b{re.escape(idx)}\b") for idx in _KNOWN_INDICES}
_COMMODITY_PATTERNS = {c: re.compile(rf"\b{re.escape(c)}\b", re.IGNORECASE) for c in _KNOWN_COMMODITIES}
_CRYPTO_PATTERNS = {k: re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in _KNOWN_CRYPTO}


@dataclass
class EntityHit:
    text: str
    kind: str  # "cashtag" | "ticker" | "currency" | "central_bank" | "index" | "commodity" | "crypto" | "company"
    source: str  # which detector found it


@dataclass
class EntityHits:
    hits: list[EntityHit] = field(default_factory=list)

    @property
    def cashtags(self) -> list[str]:
        return [h.text for h in self.hits if h.kind == "cashtag"]

    @property
    def tickers(self) -> list[str]:
        return [h.text for h in self.hits if h.kind == "ticker"]

    def unique_texts(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for h in self.hits:
            if h.text not in seen:
                seen.add(h.text)
                out.append(h.text)
        return out

    def by_kind(self, kind: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for h in self.hits:
            if h.kind == kind and h.text not in seen:
                seen.add(h.text)
                out.append(h.text)
        return out


class EntityLinker:
    """Deterministic, rule-based entity extraction. No ML required."""

    def __init__(self, company_aliases: dict[str, str] | None = None) -> None:
        # Map "Apple" → "AAPL". Provided by SymbolMapper at construction time
        # in the supervisor; here we accept any dict.
        self.company_aliases = company_aliases or {}

    def extract(self, title: str | None, text: str | None) -> EntityHits:
        title_s = (title or "").strip()
        text_s = (text or "").strip()
        joined = f"{title_s}\n{text_s}"
        if not joined.strip():
            return EntityHits()

        hits: list[EntityHit] = []
        seen_pairs: set[tuple[str, str]] = set()

        def add(text_: str, kind: str, source: str) -> None:
            key = (kind, text_)
            if key not in seen_pairs:
                seen_pairs.add(key)
                hits.append(EntityHit(text=text_, kind=kind, source=source))

        for m in _CASHTAG_RE.finditer(joined):
            add(m.group(1), "cashtag", "regex:cashtag")
        for m in _TICKER_PAREN_RE.finditer(joined):
            # Skip regulator/agency/macro acronyms (SEC, FDA, GDP, …) — the
            # bare-paren regex matches ANY 2-6 char uppercase token, so without
            # this denylist a "(SEC)"/"(FDA)" mention became a fake `ticker`
            # hit. service.process then trusts ticker hits via the equities
            # bridge (`_looks_like_equity_ticker` accepts bare uppercase) and
            # mislabels a regulatory/macro/sports story as `equities`. Share
            # symbol_mapper's `_TICKER_DENYLIST` so the two modules agree.
            token = m.group(1)
            if token in _TICKER_DENYLIST:
                continue
            add(token, "ticker", "regex:paren")

        for ccy, pattern in _CURRENCY_PATTERNS.items():
            if ccy in joined and pattern.search(joined):
                add(ccy, "currency", "lex:currency")

        joined_lc = joined.lower()

        # Word-boundary match — pre-fix the plain `in` substring check made
        # "Fed" (a central-bank token) hit inside "Federation"/"Federated"
        # and "Dow" (an index) hit inside "Down"/"shutdown". The other
        # lexicon scans below (commodities, crypto) already used \b...\b
        # — the inconsistency was the real bug. Names with non-alnum chars
        # (e.g. "S&P 500") are still matched because re.escape neutralises
        # the special chars and \b anchors on the surrounding text.
        for cb, pattern in _CENTRAL_BANK_PATTERNS.items():
            if cb in joined and pattern.search(joined):
                add(cb, "central_bank", "lex:central_bank")

        for idx, pattern in _INDEX_PATTERNS.items():
            if idx in joined and pattern.search(joined):
                add(idx, "index", "lex:index")

        for c, pattern in _COMMODITY_PATTERNS.items():
            if c.lower() in joined_lc and pattern.search(joined):
                add(c, "commodity", "lex:commodity")

        for k, pattern in _CRYPTO_PATTERNS.items():
            if k.lower() in joined_lc and pattern.search(joined):
                add(k, "crypto", "lex:crypto")

        # Company aliases must match as whole tokens. Plain substring matching
        # turns words like "unaffordable" into a false Ford/F hit.
        for alias, ticker in self.company_aliases.items():
            if alias:
                if alias.lower() not in joined_lc:
                    continue
                if _contains_alias(joined, alias):
                    add(alias, "company", "alias")
                    add(ticker, "ticker", "alias")

        return EntityHits(hits=hits)
