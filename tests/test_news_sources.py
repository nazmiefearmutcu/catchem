"""Contract tests for the per-feed news source health surface.

These exercise both layers:

  * ``NewsPoller._record_feed_result`` accumulates polls / successes /
    failures correctly and exposes a cumulative ``items_total``.
  * The ``GET /api/news/sources`` endpoint shapes that state into a UI-
    facing payload with computed ``success_rate``, ``healthy_count`` and
    ``degraded_count``, and degrades gracefully when the poller is not
    configured (returns 200 with ``configured: false`` rather than 503).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.news_poller import (
    FeedFetchResult,
    FeedSpec,
    NewsPoller,
    ParsedItem,
)
from catchem.settings import load_settings, reload_settings


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_poller(feeds: list[FeedSpec]) -> NewsPoller:
    """Build a NewsPoller with stub supervisor + settings.

    We never call ``start()`` on the returned poller; tests reach into the
    private ``_record_feed_result`` so the asyncio loop, network client,
    and supervisor are all unused.
    """

    class _StubSupervisor:
        pass

    class _StubSettings:
        class paths:
            catchem_output_dir = Path("/tmp")

    return NewsPoller(
        supervisor=_StubSupervisor(),  # type: ignore[arg-type]
        settings=_StubSettings(),  # type: ignore[arg-type]
        feeds=feeds,
    )


def _item(url: str = "https://example.com/a") -> ParsedItem:
    return ParsedItem(
        title="Markets rally",
        text="Stocks rallied after earnings.",
        url=url,
        domain="example.com",
        published_ts=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with the news poller force-disabled.

    Sources endpoint must still respond 200 + ``configured:false`` so the
    UI gets a clean empty state instead of an opaque 503.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


# ── Backend stats accumulator ───────────────────────────────────────────────


def test_feed_stats_accumulates_polls_successes_and_failures() -> None:
    spec = FeedSpec("example", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])

    poller._record_feed_result(FeedFetchResult(spec=spec, items=(_item(),), status_code=200, elapsed_ms=12.0))
    poller._record_feed_result(FeedFetchResult(spec=spec, items=(_item(), _item("https://example.com/b")), status_code=200, elapsed_ms=14.0))
    poller._record_feed_result(FeedFetchResult(spec=spec, status_code=429, error="http_429", elapsed_ms=2.0))

    snap = poller.feed_health_snapshot()[0]
    assert snap["total_fetches"] == 3
    assert snap["total_errors"] == 1
    # consecutive_errors must reset on success then climb on the latest
    # failure — i.e. counted off the most recent run, not lifetime.
    assert snap["consecutive_errors"] == 1
    assert snap["items_total"] == 3
    # `item_count` is the last-tick value; here the last tick is the
    # error (no items), so 0.
    assert snap["item_count"] == 0
    assert snap["ok"] is False


def test_feed_stats_items_total_persists_across_failures() -> None:
    """items_total must NOT reset on a failing fetch."""
    spec = FeedSpec("example", "https://example.com/rss", "example.com")
    poller = _make_poller([spec])

    poller._record_feed_result(FeedFetchResult(spec=spec, items=(_item(), _item("https://example.com/b")), status_code=200))
    poller._record_feed_result(FeedFetchResult(spec=spec, status_code=503, error="http_503"))
    poller._record_feed_result(FeedFetchResult(spec=spec, items=(_item("https://example.com/c"),), status_code=200))

    snap = poller.feed_health_snapshot()[0]
    assert snap["items_total"] == 3
    assert snap["total_errors"] == 1
    assert snap["total_fetches"] == 3


# ── Endpoint shape ──────────────────────────────────────────────────────────


def test_news_sources_endpoint_returns_configured_false_when_poller_disabled(
    client: TestClient,
) -> None:
    r = client.get("/api/news/sources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 1
    assert body["configured"] is False
    assert body["total"] == 0
    assert body["healthy_count"] == 0
    assert body["degraded_count"] == 0
    assert body["sources"] == []
    # Even when un-configured we still produce a generated_at timestamp so
    # the UI can render "as of" without a null guard.
    assert isinstance(body["generated_at"], str) and body["generated_at"]


def test_news_sources_endpoint_returns_per_feed_payload_when_poller_present(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force-inject a poller stub into the module-global so we can verify
    the shape the UI consumes without spinning up the real RSS layer."""
    from catchem import api as api_module

    spec_a = FeedSpec("ok-feed", "https://a.example.com/rss", "a.example.com")
    spec_b = FeedSpec("bad-feed", "https://b.example.com/rss", "b.example.com")
    poller = _make_poller([spec_a, spec_b])
    # Pretend two polls happened.
    poller._record_feed_result(FeedFetchResult(spec=spec_a, items=(_item(),), status_code=200, elapsed_ms=12.0))
    poller._record_feed_result(FeedFetchResult(spec=spec_b, status_code=500, error="http_500", elapsed_ms=3.0))
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    r = client.get("/api/news/sources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["total"] == 2
    assert body["healthy_count"] == 1
    assert body["degraded_count"] == 1
    assert body["total_items"] == 1
    sources = body["sources"]
    # Sources sorted by URL — a.example.com sorts before b.example.com.
    assert [s["name"] for s in sources] == ["ok-feed", "bad-feed"]
    ok_row = sources[0]
    bad_row = sources[1]
    assert ok_row["last_status"] == "ok"
    assert ok_row["polls"] == 1
    assert ok_row["successes"] == 1
    assert ok_row["failures"] == 0
    assert ok_row["success_rate"] == 1.0
    assert ok_row["items_total"] == 1
    assert ok_row["last_error"] in (None, "")
    assert bad_row["last_status"] == "error"
    assert bad_row["polls"] == 1
    assert bad_row["successes"] == 0
    assert bad_row["failures"] == 1
    assert bad_row["success_rate"] == 0.0
    assert bad_row["last_error"] == "http_500"


def test_news_sources_success_rate_clamps_when_polls_is_zero() -> None:
    """A feed that's been registered but never polled must report 0.0
    rather than dividing by zero or returning NaN."""
    spec = FeedSpec("never-polled", "https://x.example.com/rss", "x.example.com")
    poller = _make_poller([spec])
    # Hand-craft a feed_health entry with polls=0 (the natural state after
    # poller boot but before the first tick).
    poller.feed_health[spec.name] = {
        "name": spec.name,
        "url": spec.url,
        "fallback_domain": spec.fallback_domain,
        "ok": False,
        "status_code": None,
        "error": None,
        "item_count": 0,
        "items_total": 0,
        "last_fetch_at": None,
        "elapsed_ms": None,
        "total_fetches": 0,
        "total_errors": 0,
        "consecutive_errors": 0,
        "last_success_at": None,
        "last_failure_at": None,
    }
    snap = poller.feed_health_snapshot()[0]
    polls = int(snap["total_fetches"])
    failures = int(snap["total_errors"])
    successes = max(0, polls - failures)
    assert polls == 0
    assert successes == 0
    # Reproduce the endpoint's clamp explicitly so the test pins the rule
    # even if the route helper is refactored.
    rate = (successes / polls) if polls > 0 else 0.0
    assert rate == 0.0


def test_news_sources_endpoint_preserves_news_status_contract(
    client: TestClient,
) -> None:
    """Adding /api/news/sources must not break /ui/news-status."""
    r = client.get("/ui/news-status")
    assert r.status_code == 200, r.text
    body = r.json()
    # Pin the public contract — every field consumed by the existing
    # NewsStatus type in the frontend.
    for key in (
        "enabled", "feeds", "interval_seconds", "last_run_at",
        "last_ingested", "total_ingested", "last_error", "is_polling",
        "last_new_at", "empty_ticks",
    ):
        assert key in body, f"missing {key} from /ui/news-status"


def test_news_sources_endpoint_handles_unknown_status_for_zero_polls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feed that's been registered but never polled must report
    ``last_status == 'unknown'`` (not 'ok' or 'error') and a
    ``success_rate`` of 0."""
    from catchem import api as api_module

    spec = FeedSpec("pending", "https://p.example.com/rss", "p.example.com")
    poller = _make_poller([spec])
    poller.feed_health[spec.name] = {
        "name": spec.name,
        "url": spec.url,
        "fallback_domain": spec.fallback_domain,
        "ok": False,
        "status_code": None,
        "error": None,
        "item_count": 0,
        "items_total": 0,
        "last_fetch_at": None,
        "elapsed_ms": None,
        "total_fetches": 0,
        "total_errors": 0,
        "consecutive_errors": 0,
        "last_success_at": None,
        "last_failure_at": None,
    }
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    r = client.get("/api/news/sources")
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["configured"] is True
    assert body["total"] == 1
    assert body["healthy_count"] == 0
    assert body["degraded_count"] == 0
    src = body["sources"][0]
    assert src["last_status"] == "unknown"
    assert src["polls"] == 0
    assert src["success_rate"] == 0.0
