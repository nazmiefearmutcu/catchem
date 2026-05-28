"""Tests for the market reaction overlay (`catchem.quant.market_reaction`)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pytest

from catchem.quant.market_reaction import (
    HorizonReturn,
    ReactionReport,
    compute_reaction,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuote:
    """Mirror of the MarketQuote attributes the reaction layer reads."""

    symbol: str
    last: float | None
    prev_close: float | None
    error_code: str | None = None


class FakeQuoteProvider:
    """Hard-coded snapshot provider for predictable expectations."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._table: dict[str, FakeQuote] = {
            "AAPL": FakeQuote(symbol="AAPL", last=190.0, prev_close=185.0),
            "SPY": FakeQuote(symbol="SPY", last=450.0, prev_close=445.0),
            "BTC-USD": FakeQuote(symbol="BTC-USD", last=44_000.0, prev_close=43_000.0),
            "TLT": FakeQuote(symbol="TLT", last=95.0, prev_close=96.0),
        }

    def get_quote(self, symbol: str) -> FakeQuote:
        self.calls.append(symbol)
        if symbol in self._table:
            return self._table[symbol]
        return FakeQuote(
            symbol=symbol,
            last=None,
            prev_close=None,
            error_code="quote_unavailable",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _aapl_record() -> dict:
    return {
        "capture_id": "cap-aapl-1",
        "published_ts": "2026-05-27T13:00:00Z",
        "candidate_symbols": ["AAPL"],
        "asset_classes": ["equities"],
    }


def test_aapl_record_positive_excess_15m() -> None:
    """AAPL up 5/185 vs SPY up 5/445 → positive 15m excess."""
    provider = FakeQuoteProvider()
    report = compute_reaction(_aapl_record(), provider)

    expected_excess = (5 / 185 - 5 / 445) * 100.0
    assert report.headline_excess_return_15m is not None
    assert report.headline_excess_return_15m == pytest.approx(expected_excess, rel=1e-9)
    assert report.headline_excess_return_15m > 0.0
    assert report.benchmark_symbol == "SPY"
    assert report.fallback_reason is None

    # All four horizons present, all with the same snapshot delta.
    assert tuple(h.horizon for h in report.horizons) == ("5m", "15m", "1h", "1d")
    for row in report.horizons:
        assert row.symbol == "AAPL"
        assert row.last_at_t0 == pytest.approx(185.0)
        assert row.last_at_t == pytest.approx(190.0)
        assert row.return_pct == pytest.approx(5 / 185 * 100.0)
        assert row.benchmark_return_pct == pytest.approx(5 / 445 * 100.0)
        assert row.excess_return_pct == pytest.approx(expected_excess)


def test_empty_candidates_crypto_asset_class_uses_btc_usd() -> None:
    """No candidates + crypto asset class → BTC-USD proxy."""
    provider = FakeQuoteProvider()
    record = {
        "capture_id": "cap-crypto-1",
        "published_ts": None,
        "candidate_symbols": [],
        "asset_classes": ["crypto"],
    }

    report = compute_reaction(record, provider)
    assert all(row.symbol == "BTC-USD" for row in report.horizons)
    assert "BTC-USD" in provider.calls
    # BTC-USD is up vs SPY also up → excess defined.
    assert report.headline_excess_return_15m is not None
    assert report.fallback_reason is None


def test_empty_candidates_empty_asset_classes_defaults_to_spy() -> None:
    """No candidates AND no asset classes → SPY default. Excess = 0."""
    provider = FakeQuoteProvider()
    record = {
        "capture_id": "cap-default-1",
        "published_ts": None,
        "candidate_symbols": [],
        "asset_classes": [],
    }

    report = compute_reaction(record, provider)
    assert all(row.symbol == "SPY" for row in report.horizons)
    # When the primary symbol IS the benchmark, excess must be zero.
    assert report.headline_excess_return_15m == pytest.approx(0.0, abs=1e-9)


def test_quote_unavailable_sets_fallback_reason_and_none_returns() -> None:
    """Unknown symbol → returns None everywhere, fallback_reason populated."""
    provider = FakeQuoteProvider()
    record = {
        "capture_id": "cap-unknown-1",
        "published_ts": "2026-05-27T13:00:00Z",
        "candidate_symbols": ["WHO-KNOWS"],
        "asset_classes": ["equities"],
    }

    report = compute_reaction(record, provider)
    assert report.fallback_reason == "quote_unavailable"
    assert report.headline_excess_return_15m is None
    for row in report.horizons:
        assert row.symbol == "WHO-KNOWS"
        assert row.last_at_t0 is None
        assert row.last_at_t is None
        assert row.return_pct is None
        assert row.excess_return_pct is None
        # Benchmark leg still resolved cleanly.
        assert row.benchmark_return_pct == pytest.approx(5 / 445 * 100.0)


def test_report_is_cleanly_asdict_serializable() -> None:
    """`dataclasses.asdict` must round-trip without exotic types."""
    provider = FakeQuoteProvider()
    report = compute_reaction(_aapl_record(), provider)

    blob = asdict(report)
    assert isinstance(blob, dict)
    assert blob["capture_id"] == "cap-aapl-1"
    assert blob["benchmark_symbol"] == "SPY"
    # `asdict` preserves the outer container type for tuples of dataclasses.
    assert isinstance(blob["horizons"], (list, tuple))
    assert len(blob["horizons"]) == 4
    assert all(isinstance(item, dict) for item in blob["horizons"])

    # No NaN / inf / unusual types leaked through.
    def _scan(value: object) -> None:
        if isinstance(value, dict):
            for v in value.values():
                _scan(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _scan(v)
        elif isinstance(value, float):
            assert math.isfinite(value), f"non-finite float leaked: {value!r}"
        else:
            assert value is None or isinstance(value, (str, int, bool)), (
                f"exotic type leaked: {type(value).__name__}={value!r}"
            )

    _scan(blob)


def test_dataclasses_are_frozen() -> None:
    """Both dataclasses are immutable — caching/equality stays predictable."""
    row = HorizonReturn(
        horizon="5m",
        symbol="AAPL",
        last_at_t0=185.0,
        last_at_t=190.0,
        return_pct=2.7,
        benchmark_return_pct=1.1,
        excess_return_pct=1.6,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        row.symbol = "MSFT"  # type: ignore[misc]

    report = ReactionReport(
        capture_id="x",
        published_ts=None,
        horizons=(row,),
        headline_excess_return_15m=1.6,
        benchmark_symbol="SPY",
        fallback_reason=None,
    )
    with pytest.raises(Exception):
        report.benchmark_symbol = "QQQ"  # type: ignore[misc]


def test_nan_or_inf_last_treated_as_unavailable_with_no_explicit_error() -> None:
    """A provider returning ``last=NaN`` (or inf) with no ``error_code`` is
    still "unavailable" — ``_safe_float`` rejects non-finite and
    ``_extract_error_code`` synthesizes ``quote_unavailable``.

    Also covers ``_pick_primary_symbol``'s ``None`` skip on
    ``candidate_symbols`` so the entries-with-Nones case is pinned.

    Pins lines 92-93 / 100-101 / 115-119 / 142 / 148 (the defensive
    funnel that keeps a poisoned snapshot from leaking NaN into the
    JSON report).
    """

    nan_provider = type(
        "NaNQuoteProvider",
        (),
        {
            "get_quote": lambda self, symbol: FakeQuote(
                symbol=symbol,
                # NaN (==!=) and infinity both fail ``_safe_float``.
                last=float("nan") if symbol != "SPY" else 450.0,
                prev_close=float("inf") if symbol != "SPY" else 445.0,
                error_code=None,  # No explicit error → synthesize one.
            )
        },
    )()

    record = {
        "capture_id": "cap-nan",
        "published_ts": "2026-05-27T13:00:00Z",
        # First None entry exercises line 92-93 (continue past None candidate),
        # second None exercises line 100-101 (continue past None asset_class).
        "candidate_symbols": [None, "AAPL"],
        "asset_classes": [None, "equities"],
    }

    report = compute_reaction(record, nan_provider)

    # Primary symbol resolved through the None-skip.
    assert all(row.symbol == "AAPL" for row in report.horizons)
    # NaN snapshot ⇒ no explicit error_code, but _extract_error_code
    # synthesizes "quote_unavailable" via the None-last fallback.
    assert report.fallback_reason == "quote_unavailable"
    for row in report.horizons:
        assert row.last_at_t0 is None
        assert row.last_at_t is None
        assert row.return_pct is None
        assert row.excess_return_pct is None


def test_safe_float_rejects_garbage_and_zero_prev_close_returns_none() -> None:
    """Non-numeric ``last`` and zero ``prev_close`` both collapse the
    snapshot to ``None`` returns without raising.

    Covers ``_safe_float`` TypeError/ValueError branch (lines 115-116)
    and the ``prev == 0.0`` branch in ``_snapshot_return_pct``
    (line 135-136) — neither should crash the engine path.
    """

    bad_provider = type(
        "BadFloatProvider",
        (),
        {
            "get_quote": lambda self, symbol: FakeQuote(
                symbol=symbol,
                # str passed where float expected → TypeError in _safe_float.
                last="not-a-number" if symbol == "AAPL" else 450.0,
                # 0.0 prev_close is real (a halted ticker) but div-by-zero
                # must short-circuit to None rather than throw.
                prev_close=0.0 if symbol == "AAPL" else 445.0,
                error_code=None,
            )
        },
    )()

    record = {
        "capture_id": "cap-bad",
        "published_ts": "2026-05-27T13:00:00Z",
        "candidate_symbols": ["AAPL"],
        "asset_classes": ["equities"],
    }

    report = compute_reaction(record, bad_provider)

    # last is non-numeric → _safe_float returns None → quote treated as
    # unavailable, fallback_reason synthesized.
    assert report.fallback_reason == "quote_unavailable"
    for row in report.horizons:
        assert row.return_pct is None
