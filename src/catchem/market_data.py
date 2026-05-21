"""Local-first market quote provider.

Round 12 intentionally ships no third-party integration. The only provider here
is a deterministic fixture snapshot so the API has a typed contract without
claiming live market data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .contracts import MarketQuote

FIXTURE_PROVIDER = "local_fixture"
MARKET_STATE_FIXTURE = "fixture_snapshot"
FRESHNESS_STALE = "stale"
FRESHNESS_UNAVAILABLE = "unavailable"
ERROR_UNKNOWN_SYMBOL = "quote_unavailable"


@dataclass(frozen=True)
class FixtureQuote:
    symbol: str
    currency: str
    last: float
    prev_close: float
    as_of: datetime


_FIXTURE_QUOTES: dict[str, FixtureQuote] = {
    "AAPL": FixtureQuote(
        symbol="AAPL",
        currency="USD",
        last=189.98,
        prev_close=188.85,
        as_of=datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc),
    ),
    "MSFT": FixtureQuote(
        symbol="MSFT",
        currency="USD",
        last=370.60,
        prev_close=369.14,
        as_of=datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc),
    ),
    "BTCUSD": FixtureQuote(
        symbol="BTCUSD",
        currency="USD",
        last=44120.50,
        prev_close=43890.00,
        as_of=datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc),
    ),
}


def normalize_symbol(symbol: str) -> str:
    return "".join(str(symbol or "").upper().strip().split())


def parse_symbol_list(symbols: str | Iterable[str]) -> list[str]:
    raw: Iterable[str]
    if isinstance(symbols, str):
        raw = symbols.split(",")
    else:
        raw = symbols
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        sym = normalize_symbol(str(item))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


class LocalFixtureMarketDataProvider:
    """Deterministic quote provider with explicit stale/unavailable semantics."""

    provider = FIXTURE_PROVIDER
    stale_window = timedelta(minutes=15)

    def quote(self, symbol: str, *, now: datetime | None = None) -> MarketQuote:
        retrieved_at = now or datetime.now(timezone.utc)
        sym = normalize_symbol(symbol)
        fixture = _FIXTURE_QUOTES.get(sym)
        if fixture is None:
            return MarketQuote(
                symbol=sym,
                provider=self.provider,
                as_of=None,
                retrieved_at=retrieved_at.isoformat(),
                currency=None,
                last=None,
                prev_close=None,
                change_abs=None,
                change_pct=None,
                market_state="unavailable",
                stale_after=None,
                freshness_status=FRESHNESS_UNAVAILABLE,
                error_code=ERROR_UNKNOWN_SYMBOL,
            )

        change_abs = fixture.last - fixture.prev_close
        change_pct = change_abs / fixture.prev_close if fixture.prev_close else None
        stale_after = fixture.as_of + self.stale_window
        return MarketQuote(
            symbol=fixture.symbol,
            provider=self.provider,
            as_of=fixture.as_of.isoformat(),
            retrieved_at=retrieved_at.isoformat(),
            currency=fixture.currency,
            last=fixture.last,
            prev_close=fixture.prev_close,
            change_abs=change_abs,
            change_pct=change_pct,
            market_state=MARKET_STATE_FIXTURE,
            stale_after=stale_after.isoformat(),
            freshness_status=FRESHNESS_STALE,
            error_code=None,
        )

    def quotes(self, symbols: Iterable[str], *, now: datetime | None = None) -> list[MarketQuote]:
        retrieved_at = now or datetime.now(timezone.utc)
        return [self.quote(symbol, now=retrieved_at) for symbol in symbols]


__all__ = [
    "ERROR_UNKNOWN_SYMBOL",
    "FIXTURE_PROVIDER",
    "FRESHNESS_STALE",
    "FRESHNESS_UNAVAILABLE",
    "LocalFixtureMarketDataProvider",
    "MARKET_STATE_FIXTURE",
    "normalize_symbol",
    "parse_symbol_list",
]
