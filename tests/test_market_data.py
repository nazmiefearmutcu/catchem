from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.contracts import MarketQuote
from catchem.market_data import (
    ERROR_UNKNOWN_SYMBOL,
    FIXTURE_PROVIDER,
    FRESHNESS_STALE,
    FRESHNESS_UNAVAILABLE,
    MARKET_STATE_FIXTURE,
    LocalFixtureMarketDataProvider,
    normalize_symbol,
    parse_symbol_list,
)
from catchem.settings import load_settings


def test_market_quote_contract_shape_for_fixture() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote("AAPL", now=datetime(2026, 5, 21, 12, 0, tzinfo=UTC))
    payload = quote.model_dump()

    assert set(payload) == {
        "symbol",
        "provider",
        "as_of",
        "retrieved_at",
        "currency",
        "last",
        "prev_close",
        "change_abs",
        "change_pct",
        "market_state",
        "stale_after",
        "freshness_status",
        "error_code",
    }
    assert MarketQuote(**payload).symbol == "AAPL"
    assert payload["provider"] == "local_fixture"
    assert payload["freshness_status"] == "stale"
    assert payload["market_state"] == "fixture_snapshot"


def test_market_quote_unknown_symbol_is_typed_unavailable() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote("NO_SUCH_SYMBOL")

    assert quote.symbol == "NO_SUCH_SYMBOL"
    assert quote.provider == "local_fixture"
    assert quote.freshness_status == "unavailable"
    assert quote.market_state == "unavailable"
    assert quote.error_code == "quote_unavailable"
    assert quote.last is None


def test_parse_symbol_list_trims_dedupes_and_skips_empty_values() -> None:
    assert parse_symbol_list(" aapl, MSFT ,,aapl, btc usd ") == ["AAPL", "MSFT", "BTCUSD"]


def test_batched_quotes_endpoint_returns_known_and_unavailable_items() -> None:
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/ui/quotes?symbols=AAPL, MSFT,NOPE,AAPL")

    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "local_fixture"
    assert [item["symbol"] for item in body["items"]] == ["AAPL", "MSFT", "NOPE"]
    assert body["items"][0]["freshness_status"] == "stale"
    assert body["items"][2]["freshness_status"] == "unavailable"
    assert body["items"][2]["error_code"] == "quote_unavailable"


def test_tauri_boot_origin_can_fetch_healthz() -> None:
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/healthz", headers={"Origin": "tauri://localhost"})

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "tauri://localhost"


def test_single_quote_endpoint_does_not_500_for_unknown_symbol() -> None:
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/ui/quote/UNKNOWN")

    assert r.status_code == 200
    assert r.json()["freshness_status"] == "unavailable"


def test_market_data_does_not_leak_chart_context(monkeypatch) -> None:
    import catchem.chart_context as chart_context

    def fail_if_used(*args, **kwargs):
        raise AssertionError("chart_context must not back market quote endpoints")

    monkeypatch.setattr(chart_context, "ChartContextReader", fail_if_used)
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/ui/quote/AAPL")

    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "local_fixture"
    assert "source_path" not in body


def test_known_quote_computes_change_abs_and_pct_from_fixture_math() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote("AAPL", now=datetime(2026, 5, 21, 12, 0, tzinfo=UTC))

    # last 189.98, prev_close 188.85 -> +1.13 abs, +0.598...% as a ratio.
    assert quote.last == 189.98
    assert quote.prev_close == 188.85
    assert quote.change_abs is not None
    assert abs(quote.change_abs - (189.98 - 188.85)) < 1e-9
    assert quote.change_pct is not None
    assert abs(quote.change_pct - (189.98 - 188.85) / 188.85) < 1e-9
    assert quote.currency == "USD"
    assert quote.error_code is None


def test_known_quote_sets_stale_after_fifteen_minutes_past_as_of() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote("MSFT")

    # as_of is 2024-01-02T21:00:00Z; stale_window is 15 minutes.
    assert quote.as_of == "2024-01-02T21:00:00+00:00"
    assert quote.stale_after == "2024-01-02T21:15:00+00:00"
    assert quote.freshness_status == FRESHNESS_STALE
    assert quote.market_state == MARKET_STATE_FIXTURE


def test_quote_retrieved_at_uses_supplied_now() -> None:
    provider = LocalFixtureMarketDataProvider()
    now = datetime(2026, 5, 21, 9, 30, tzinfo=UTC)

    known = provider.quote("AAPL", now=now)
    unknown = provider.quote("ZZZZ", now=now)

    assert known.retrieved_at == now.isoformat()
    # The unavailable path must still stamp retrieved_at with the same clock.
    assert unknown.retrieved_at == now.isoformat()


def test_quote_lowercase_and_padded_symbol_is_normalized_to_fixture() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote("  aapl  ")

    assert quote.symbol == "AAPL"
    assert quote.freshness_status == FRESHNESS_STALE
    assert quote.error_code is None


def test_unknown_symbol_normalizes_before_reporting_unavailable() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote(" no such ")

    # Whitespace is collapsed/upper-cased even on the failure path.
    assert quote.symbol == "NOSUCH"
    assert quote.provider == FIXTURE_PROVIDER
    assert quote.freshness_status == FRESHNESS_UNAVAILABLE
    assert quote.error_code == ERROR_UNKNOWN_SYMBOL
    assert quote.as_of is None
    assert quote.stale_after is None
    assert quote.change_abs is None
    assert quote.change_pct is None


def test_quotes_plural_preserves_order_and_shares_retrieved_at() -> None:
    provider = LocalFixtureMarketDataProvider()
    now = datetime(2026, 5, 21, 18, 0, tzinfo=UTC)

    results = provider.quotes(["AAPL", "NOPE", "BTCUSD"], now=now)

    assert [q.symbol for q in results] == ["AAPL", "NOPE", "BTCUSD"]
    assert [q.freshness_status for q in results] == [
        FRESHNESS_STALE,
        FRESHNESS_UNAVAILABLE,
        FRESHNESS_STALE,
    ]
    # Every item in a batch is stamped with the one shared clock value.
    assert {q.retrieved_at for q in results} == {now.isoformat()}


def test_quotes_plural_empty_iterable_returns_empty_list() -> None:
    provider = LocalFixtureMarketDataProvider()
    assert provider.quotes([]) == []


def test_normalize_symbol_handles_none_and_internal_whitespace() -> None:
    assert normalize_symbol(None) == ""  # type: ignore[arg-type]
    assert normalize_symbol("  ") == ""
    assert normalize_symbol(" btc usd ") == "BTCUSD"
    assert normalize_symbol("aapl") == "AAPL"


def test_parse_symbol_list_accepts_an_iterable_of_strings() -> None:
    # Exercises the non-str branch: a list (not a comma string) is iterated
    # directly, normalized, de-duplicated and emptied-out item by item.
    assert parse_symbol_list([" aapl ", "MSFT", "aapl", "", "btc usd"]) == [
        "AAPL",
        "MSFT",
        "BTCUSD",
    ]


def test_market_data_endpoint_has_no_third_party_network_dependency(monkeypatch) -> None:
    def blocked_network(*args, **kwargs):
        raise AssertionError("market data endpoint attempted network access")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", blocked_network)
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        monkeypatch.setattr(requests.sessions.Session, "request", blocked_network)
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/ui/quotes?symbols=AAPL,NOPE")

    assert r.status_code == 200
    assert [item["freshness_status"] for item in r.json()["items"]] == ["stale", "unavailable"]
