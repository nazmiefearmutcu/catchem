from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.contracts import MarketQuote
from catchem.market_data import LocalFixtureMarketDataProvider, parse_symbol_list
from catchem.settings import load_settings


def test_market_quote_contract_shape_for_fixture() -> None:
    provider = LocalFixtureMarketDataProvider()
    quote = provider.quote("AAPL", now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc))
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


def test_single_quote_endpoint_does_not_500_for_unknown_symbol() -> None:
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/ui/quote/UNKNOWN")

    assert r.status_code == 200
    assert r.json()["freshness_status"] == "unavailable"


def test_market_data_does_not_leak_chart_context(monkeypatch) -> None:
    import catchem.chart_context as chart_context

    def fail_if_used(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("chart_context must not back market quote endpoints")

    monkeypatch.setattr(chart_context, "ChartContextReader", fail_if_used)
    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.get("/ui/quote/AAPL")

    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "local_fixture"
    assert "source_path" not in body


def test_market_data_endpoint_has_no_third_party_network_dependency(monkeypatch) -> None:
    def blocked_network(*args, **kwargs):  # noqa: ANN002, ANN003
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
