"""Market reaction overlay for catchem records.

Estimates a coarse post-publication price reaction for a `FinancialImpactRecord`
by reading a current quote snapshot from a duck-typed quote provider and
comparing it against `prev_close` as a stand-in for the t0 anchor. The
horizon axis ("5m", "15m", "1h", "1d") is preserved in the report shape so
the UI can render reaction strips once a richer (bar-aware) provider is
wired in later — today every horizon reads the same snapshot delta, which
is honest given that the local fixture provider does not yet carry
intraday history.

Design notes:
  * Provider is accepted via duck typing — anything exposing
    ``get_quote(symbol) -> MarketQuote-like`` works (so tests can inject a
    FakeQuoteProvider without depending on `market_data.py`).
  * "Quote unavailable" is a normal return state, not an exception: the
    report keeps a stable shape and `fallback_reason` is populated with
    the provider's error code.
  * Returns are expressed as percent (i.e. +2.5 means +2.5%).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "HorizonReturn",
    "ReactionReport",
    "compute_reaction",
]


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HorizonReturn:
    horizon: str
    symbol: str
    last_at_t0: float | None
    last_at_t: float | None
    return_pct: float | None
    benchmark_return_pct: float | None
    excess_return_pct: float | None


@dataclass(frozen=True)
class ReactionReport:
    capture_id: str
    published_ts: str | None
    horizons: tuple[HorizonReturn, ...]
    headline_excess_return_15m: float | None
    benchmark_symbol: str
    fallback_reason: str | None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# Asset-class → proxy symbol when the record carries no candidate symbols.
_ASSET_CLASS_PROXY: dict[str, str] = {
    "equities": "SPY",
    "indices": "SPY",
    "rates": "TLT",
    "fx": "UUP",
    "credit": "LQD",
    "commodities": "DJP",
    "crypto": "BTC-USD",
    "macro": "SPY",
}

_DEFAULT_PROXY = "SPY"


class _QuoteLike(Protocol):
    """Minimal shape we need from whatever the provider returns."""

    last: float | None
    prev_close: float | None
    error_code: str | None


def _pick_primary_symbol(record: dict) -> str:
    """Pick the first non-empty candidate symbol, else an asset-class proxy."""
    candidates = record.get("candidate_symbols")
    if candidates:
        for raw in candidates:
            if raw is None:
                continue
            sym = raw.strip().upper() if type(raw) is str else str(raw).strip().upper()
            if sym:
                return sym

    asset_classes = record.get("asset_classes")
    if asset_classes:
        for raw in asset_classes:
            if raw is None:
                continue
            key = raw.strip().lower() if type(raw) is str else str(raw).strip().lower()
            if key in _ASSET_CLASS_PROXY:
                return _ASSET_CLASS_PROXY[key]

    return _DEFAULT_PROXY


def _safe_float(value: Any) -> float | None:
    """Best-effort float coercion; treat NaN/inf/garbage as None."""
    val_type = type(value)
    if val_type is float:
        if math.isfinite(value):
            return value
        return None
    if val_type is int:
        return float(value)
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(out):
        return out
    return None


def _snapshot_return_pct(quote: _QuoteLike | None) -> tuple[float | None, float | None, float | None]:
    """Return (last_at_t0, last_at_t, return_pct) from a snapshot quote.

    ``last_at_t0`` is the previous close (anchor) and ``last_at_t`` is the
    current last price. Percent return uses the standard
    ``(last - prev_close) / prev_close * 100`` form.
    """
    if quote is None:
        return None, None, None

    last = _safe_float(getattr(quote, "last", None))
    prev = _safe_float(getattr(quote, "prev_close", None))
    if last is None or prev is None or prev == 0.0:
        return prev, last, None
    return prev, last, (last - prev) / prev * 100.0


def _extract_error_code(quote: _QuoteLike | None, sym_t: float | None) -> str | None:
    if quote is None:
        return "quote_unavailable"
    code = getattr(quote, "error_code", None)
    if code:
        return code if type(code) is str else str(code)
    # Even if no explicit error_code, a None last counts as unavailable.
    if sym_t is None:
        return "quote_unavailable"
    return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def compute_reaction(
    record: dict,
    quote_provider: Any,
    *,
    horizons: tuple[str, ...] = ("5m", "15m", "1h", "1d"),
    benchmark_symbol: str = "SPY",
) -> ReactionReport:
    """Build a `ReactionReport` for ``record`` using ``quote_provider``.

    Parameters
    ----------
    record:
        A FinancialImpactRecord dict. Reads ``capture_id``, ``published_ts``,
        ``candidate_symbols``, ``asset_classes``.
    quote_provider:
        Any object with ``get_quote(symbol) -> MarketQuote-like``. The
        returned object only needs ``last``, ``prev_close``, ``error_code``
        attributes.
    horizons:
        Horizon labels to populate in the report. Today every entry reads
        the same snapshot — the axis is preserved so the UI can render
        reaction strips when bar-aware data is wired in later.
    benchmark_symbol:
        Symbol used to compute excess return. Defaults to "SPY".

    Returns
    -------
    ReactionReport
        Stable shape regardless of provider state. When the symbol's quote
        is unavailable, every horizon's return fields are ``None`` and
        ``fallback_reason`` carries the provider error code.
    """
    capture_id_raw = record.get("capture_id")
    capture_id = capture_id_raw if type(capture_id_raw) is str else str(capture_id_raw or "")
    published_ts_raw = record.get("published_ts")
    published_ts = (
        published_ts_raw
        if type(published_ts_raw) is str
        else (str(published_ts_raw) if published_ts_raw else None)
    )

    symbol = _pick_primary_symbol(record)
    bench_raw = benchmark_symbol or _DEFAULT_PROXY
    bench = bench_raw.strip().upper() if type(bench_raw) is str else str(bench_raw).strip().upper()
    if not bench:
        bench = _DEFAULT_PROXY

    # One fetch per side — replicated across horizons since the snapshot
    # provider is horizon-agnostic.
    symbol_quote = quote_provider.get_quote(symbol)
    bench_quote = quote_provider.get_quote(bench) if bench else None

    sym_t0, sym_t, sym_ret = _snapshot_return_pct(symbol_quote)
    _, _, bench_ret = _snapshot_return_pct(bench_quote)

    fallback_reason = _extract_error_code(symbol_quote, sym_t)
    # If the symbol quote is bad, suppress returns entirely (we shouldn't
    # claim an excess number when we have no anchor for the primary leg).
    if fallback_reason is not None:
        sym_t0 = None
        sym_t = None
        sym_ret = None
    excess = None
    if sym_ret is not None and bench_ret is not None:
        excess = sym_ret - bench_ret

    rows = [
        HorizonReturn(
            horizon=horizon if type(horizon) is str else str(horizon),
            symbol=symbol,
            last_at_t0=sym_t0,
            last_at_t=sym_t,
            return_pct=sym_ret,
            benchmark_return_pct=bench_ret,
            excess_return_pct=excess,
        )
        for horizon in horizons
    ]

    headline = None
    for h in horizons:
        if (h if type(h) is str else str(h)) == "15m":
            headline = excess
            break

    return ReactionReport(
        capture_id=capture_id,
        published_ts=published_ts,
        horizons=tuple(rows),
        headline_excess_return_15m=headline,
        benchmark_symbol=bench,
        fallback_reason=fallback_reason,
    )
