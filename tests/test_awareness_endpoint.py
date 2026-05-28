"""Contract tests for the live "awareness window" endpoint.

``GET /api/news/awareness`` answers the analyst's "how fresh / how broad is
awareness right now?" question. These pins guard:

  * The full JSON envelope shape (every field the UI panel reads).
  * ``sources_by_parser`` tallies the configured feeds by their ``.parser``
    attribute (rss + any source-pack parsers).
  * ``window_estimate_seconds`` ≈ poll_interval + median_publisher_lag, and
    is null when no fresh median lag is available this tick.
  * The degraded path: when the poller is disabled the endpoint returns 200
    (not 503) with ``sources_total: 0`` and null lags / window.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.news_poller import FeedSpec, NewsPoller
from catchem.settings import load_settings, reload_settings


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_poller(feeds: list[FeedSpec]) -> NewsPoller:
    """Build a NewsPoller with stub supervisor + settings.

    We never call ``start()`` — the awareness endpoint only reads public
    accessors + in-memory stat fields, so the asyncio loop / network client
    / supervisor are all unused.
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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with the news poller force-disabled.

    The awareness endpoint must still respond 200 + ``configured:false`` so
    the UI gets a clean degraded panel instead of an opaque 503.
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


# ── Degraded path ─────────────────────────────────────────────────────────────


def test_awareness_returns_degraded_envelope_when_poller_disabled(
    client: TestClient,
) -> None:
    r = client.get("/api/news/awareness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 1
    assert body["configured"] is False
    assert body["sources_total"] == 0
    assert body["sources_by_parser"] == {}
    assert body["poll_interval_seconds"] is None
    assert body["median_publisher_lag_seconds"] is None
    assert body["avg_publisher_lag_seconds"] is None
    assert body["last_run_at"] is None
    assert body["last_new_at"] is None
    assert body["total_ingested"] == 0
    assert body["window_estimate_seconds"] is None
    # Always produce a generated_at so the UI can render "as of" with no guard.
    assert isinstance(body["generated_at"], str) and body["generated_at"]


# ── Full envelope + parser tally + window estimate ────────────────────────────


def test_awareness_full_envelope_and_window_estimate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a poller stub so we can verify the shape + math the UI reads
    without spinning up the real RSS layer."""
    from catchem import api as api_module

    feeds = [
        FeedSpec("rss-a", "https://a.example.com/rss", "a.example.com"),
        FeedSpec("rss-b", "https://b.example.com/rss", "b.example.com"),
        FeedSpec("gdelt-1", "https://g.example.com/json", "g.example.com", parser="gdelt"),
        FeedSpec("reddit-1", "https://r.example.com/.json", "r.example.com", parser="reddit"),
    ]
    poller = _make_poller(feeds)
    # Simulate one tick's worth of stats.
    poller.last_run_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    poller.last_new_at = datetime(2026, 5, 28, 11, 59, tzinfo=UTC)
    poller.total_ingested = 137
    poller.last_median_publisher_lag_seconds = 90.0
    poller.last_avg_publisher_lag_seconds = 120.0
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    r = client.get("/api/news/awareness")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["configured"] is True
    assert body["sources_total"] == 4
    # Tally by parser: 2 rss + 1 gdelt + 1 reddit.
    assert body["sources_by_parser"] == {"rss": 2, "gdelt": 1, "reddit": 1}
    # interval is clamped to a 10s floor at construction time.
    assert body["poll_interval_seconds"] == poller.interval_seconds
    assert body["median_publisher_lag_seconds"] == 90.0
    assert body["avg_publisher_lag_seconds"] == 120.0
    assert body["total_ingested"] == 137
    assert body["last_run_at"] == "2026-05-28T12:00:00+00:00"
    assert body["last_new_at"] == "2026-05-28T11:59:00+00:00"
    # window_estimate ≈ poll_interval + median_publisher_lag.
    assert body["window_estimate_seconds"] == poller.interval_seconds + 90.0


def test_awareness_window_is_null_without_fresh_median_lag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the poller has no fresh median lag (quiet tick), the window
    estimate must be null rather than silently equal to the poll interval —
    mirrors the poller's own 'don't show stale lag' rule."""
    from catchem import api as api_module

    poller = _make_poller([FeedSpec("rss-a", "https://a.example.com/rss", "a.example.com")])
    poller.last_median_publisher_lag_seconds = None
    poller.last_avg_publisher_lag_seconds = None
    monkeypatch.setattr(api_module, "_NEWS_POLLER", poller, raising=False)

    r = client.get("/api/news/awareness")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["sources_total"] == 1
    assert body["sources_by_parser"] == {"rss": 1}
    assert body["median_publisher_lag_seconds"] is None
    assert body["window_estimate_seconds"] is None


def test_awareness_does_not_break_news_status_contract(client: TestClient) -> None:
    """Adding /api/news/awareness must not perturb /ui/news-status."""
    r = client.get("/ui/news-status")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("enabled", "feeds", "interval_seconds", "total_ingested"):
        assert key in body, f"missing {key} from /ui/news-status"
