"""Stage F (part 2): map entity hits → trading symbols.

The mapper is data-driven. It loads aliases from any of three places, in order:
  1. catchem/configs/symbols.yaml (if present)
  2. <newsimpact>/manifests/**/symbol*.json (read-only discovery)
  3. an internal hardcoded mini-registry of high-traffic names (always available)

If NewsImpact resources are missing the mapper degrades to the internal registry
and continues — it never fails the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from rapidfuzz import fuzz, process

from .logging import get_logger

logger = get_logger("catchem.symbol_mapper")


_PAREN_TICKER_RE = re.compile(r"\(([A-Z]{1,6}(?:\.[A-Z])?)\)")
_TICKER_DENYLIST = {
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETFS", "GDP", "CPI", "PPI",
    "SEC", "FTC", "FDA", "FOMC", "ECB", "BOJ", "BOE", "PBOC", "RBA",
}


_INTERNAL_REGISTRY: Mapping[str, str] = {
    # Equity giants
    "Apple": "AAPL", "Microsoft": "MSFT", "Alphabet": "GOOGL", "Google": "GOOGL",
    "Amazon": "AMZN", "Meta Platforms": "META", "Meta": "META", "Facebook": "META",
    "Nvidia": "NVDA", "NVIDIA": "NVDA", "Tesla": "TSLA",
    "Berkshire Hathaway": "BRK.B", "Berkshire": "BRK.B",
    "JPMorgan": "JPM", "JPMorgan Chase": "JPM", "Goldman Sachs": "GS",
    "Bank of America": "BAC", "Citigroup": "C", "Morgan Stanley": "MS",
    "Wells Fargo": "WFC", "BlackRock": "BLK",
    "ExxonMobil": "XOM", "Exxon": "XOM", "Chevron": "CVX", "Shell": "SHEL", "BP": "BP",
    "Johnson & Johnson": "JNJ", "Pfizer": "PFE", "Moderna": "MRNA", "Eli Lilly": "LLY",
    "Procter & Gamble": "PG", "Coca-Cola": "KO", "PepsiCo": "PEP",
    "Walmart": "WMT", "Costco": "COST", "Home Depot": "HD", "McDonald's": "MCD",
    "Intel": "INTC", "AMD": "AMD", "Qualcomm": "QCOM", "Broadcom": "AVGO", "Cisco": "CSCO",
    "Oracle": "ORCL", "Salesforce": "CRM", "Adobe": "ADBE", "Netflix": "NFLX", "Disney": "DIS",
    "Visa": "V", "Mastercard": "MA", "PayPal": "PYPL",
    "Boeing": "BA", "Lockheed Martin": "LMT", "General Electric": "GE", "Ford": "F",
    "General Motors": "GM", "Toyota": "TM",
    # Crypto
    "Bitcoin": "BTC-USD", "BTC": "BTC-USD",
    "Ethereum": "ETH-USD", "Ether": "ETH-USD", "ETH": "ETH-USD",
    "Solana": "SOL-USD", "SOL": "SOL-USD",
    "Ripple": "XRP-USD", "XRP": "XRP-USD",
    "Cardano": "ADA-USD", "ADA": "ADA-USD",
    "Dogecoin": "DOGE-USD", "DOGE": "DOGE-USD",
    "Polkadot": "DOT-USD", "DOT": "DOT-USD",
    # Indices (Yahoo-style)
    "S&P 500": "^GSPC", "Dow Jones": "^DJI", "Nasdaq": "^IXIC", "Russell 2000": "^RUT",
    "FTSE 100": "^FTSE", "DAX": "^GDAXI", "Nikkei 225": "^N225", "Hang Seng": "^HSI",
    "VIX": "^VIX", "BIST 100": "XU100.IS",
    # FX pairs (compact)
    "EUR/USD": "EURUSD=X", "USD/JPY": "USDJPY=X", "GBP/USD": "GBPUSD=X",
    # Commodities
    "Brent": "BZ=F", "WTI": "CL=F", "gold": "GC=F", "silver": "SI=F", "copper": "HG=F",
}


def _alias_pattern(alias: str) -> re.Pattern[str]:
    # Company names and instruments must match as tokens, not as substrings of
    # unrelated words such as "unaffordable" -> "Ford".
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class SymbolMatch:
    text: str
    symbol: str
    score: float
    source: str


class SymbolMapper:
    """Alias-to-ticker resolver. Public API: ``map_text``, ``alias_dict``."""

    def __init__(
        self,
        config_path: Path | None = None,
        newsimpact_root: Path | None = None,
    ) -> None:
        self._aliases: dict[str, str] = dict(_INTERNAL_REGISTRY)
        if config_path is not None and config_path.exists():
            self._merge_yaml(config_path)
        if newsimpact_root is not None:
            self._maybe_merge_newsimpact(newsimpact_root)
        # Precompute lowercase index
        self._alias_lc = {k.lower(): v for k, v in self._aliases.items()}
        self._alias_keys = list(self._aliases.keys())
        self._alias_exact_patterns = {
            alias_lc: _alias_pattern(alias_lc)
            for alias_lc in self._alias_lc
            if len(alias_lc.strip()) > 1
        }

    def alias_dict(self) -> Mapping[str, str]:
        return dict(self._aliases)

    def map_text(self, text: str, min_fuzzy: float = 0.92) -> list[SymbolMatch]:
        if not text:
            return []
        out: list[SymbolMatch] = []
        seen: set[str] = set()
        # exact substring + cashtag
        for m in re.finditer(r"\$([A-Z]{1,6})\b", text):
            sym = m.group(1)
            if sym not in seen:
                seen.add(sym)
                out.append(SymbolMatch(text=f"${sym}", symbol=sym, score=1.0, source="cashtag"))
        for m in _PAREN_TICKER_RE.finditer(text):
            sym = m.group(1)
            if sym in _TICKER_DENYLIST:
                continue
            if sym not in seen:
                seen.add(sym)
                out.append(SymbolMatch(text=f"({sym})", symbol=sym, score=0.98, source="paren_ticker"))
        lc = text.lower()
        for alias_lc, sym in self._alias_lc.items():
            pattern = self._alias_exact_patterns.get(alias_lc)
            if pattern is not None and pattern.search(lc) and sym not in seen:
                seen.add(sym)
                out.append(SymbolMatch(text=alias_lc, symbol=sym, score=1.0, source="alias_exact"))
        # Fuzzy fallback for the title (top-3 only)
        if len(out) < 3 and len(text) < 400:
            extracted = process.extract(text, self._alias_keys, scorer=fuzz.partial_ratio, limit=3)
            for alias, score, _ in extracted:
                if len(alias.strip()) < 5:
                    continue
                if score / 100.0 < min_fuzzy:
                    continue
                sym = self._aliases[alias]
                if sym in seen:
                    continue
                seen.add(sym)
                out.append(SymbolMatch(text=alias, symbol=sym, score=score / 100.0, source="alias_fuzzy"))
        return out

    # ── loaders ──────────────────────────────────────────────────────────────
    def _merge_yaml(self, path: Path) -> None:
        import yaml
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            extra = data.get("aliases") if isinstance(data, dict) else None
            if isinstance(extra, dict):
                self._aliases.update({str(k): str(v) for k, v in extra.items()})
        except Exception as exc:
            logger.warning("symbol_yaml_failed", path=str(path), err=str(exc))

    def _maybe_merge_newsimpact(self, root: Path) -> None:
        """Read-only discovery of NewsImpact aliases. NEVER writes anything."""
        if not root.exists():
            return
        # Look for any JSON under manifests/** that has an "aliases" mapping.
        scanned = 0
        for p in root.glob("manifests/**/*.json"):
            if scanned >= 12:
                break
            scanned += 1
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            aliases = data.get("aliases") or data.get("symbol_aliases") or {}
            if isinstance(aliases, dict):
                for k, v in aliases.items():
                    self._aliases.setdefault(str(k), str(v))
        logger.info("symbol_mapper_loaded", count=len(self._aliases))
