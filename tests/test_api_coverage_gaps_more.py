from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from catchem.api import (
    _git_branch_safe,
    _git_sha_safe,
    create_app,
)
from catchem.settings import Settings


def test_git_helpers_more():
    # Test case when returncode is non-zero
    mock_res_fail = MagicMock(returncode=1, stdout="some error")
    with patch("subprocess.run", return_value=mock_res_fail):
        assert _git_sha_safe() is None
        assert _git_branch_safe() is None

    # Test case when stdout is empty
    mock_res_empty = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=mock_res_empty):
        assert _git_sha_safe() is None
        assert _git_branch_safe() is None


def test_create_app_more_cors_and_static():
    settings = Settings()
    settings.api.cors_origins = []
    with patch("catchem.api.get_static_path", return_value=None):
        app = create_app(settings)
        assert app is not None

    mock_path = MagicMock()
    mock_path.parent.__truediv__.return_value.is_dir.return_value = False
    with patch("catchem.api.get_static_path", return_value=mock_path):
        app = create_app(settings)
        assert app is not None


def test_live_read_has_signal_value_error():
    # Trigger ValueError inside nested _live_read_has_signal by returning non-int for window_records
    app = create_app(Settings())
    client = TestClient(app)
    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {
        "n_records_window": "invalid_int",  # this will trigger TypeError/ValueError in int() cast
        "n_clusters": 0,
    }
    mock_sup = MagicMock()
    mock_sup.reviewers.deepseek.return_value = None

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["fallback_reason"] == "empty_context"


def test_healthz_poller_healthy():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    mock_poller = MagicMock()
    mock_poller.last_run_at = datetime.now(UTC)
    mock_poller.interval_seconds = 60
    mock_sup = MagicMock()

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (1,)
    mock_sup.storage._connection.return_value.__enter__.return_value = mock_conn

    with (
        patch("catchem.api._NEWS_POLLER", mock_poller),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.migrations.current_version", return_value=5),
        patch("catchem.migrations.max_known_version", return_value=5),
    ):
        r = client.get("/api/health/deep")
        assert r.status_code == 200
        body = r.json()
        assert body["checks"]["news_poller_ok"] is True


def test_api_stats_budget_success():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []

    mock_registry = MagicMock()
    mock_registry.budget_state.return_value.spent_usd = 12.34
    mock_sup.reviewers = mock_registry

    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.side_effect = [(10,), (5,), (1,)]
    mock_sup.storage._connection.return_value.__enter__.return_value = mock_conn

    from catchem.api import _STATS_CACHE

    _STATS_CACHE.clear()

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["reviewers"]["deepseek_usd_spent"] == 12.34


def test_exceptions_raising_http_400():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.get_record.return_value = {"capture_id": "test_cap"}
    mock_sup.storage.add_record_tag.side_effect = ValueError("mock ValueError")
    mock_sup.storage.remove_record_tag.side_effect = ValueError("mock ValueError")
    mock_sup.storage.records_by_tag.side_effect = ValueError("mock ValueError")
    mock_sup.storage.add_holding.side_effect = ValueError("mock ValueError")

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r1 = client.post("/api/records/test_cap/tags", json={"tag": "watch"})
        assert r1.status_code == 400
        assert r1.json()["detail"] == "mock ValueError"

        r2 = client.delete("/api/records/test_cap/tags/watch")
        assert r2.status_code == 400
        assert r2.json()["detail"] == "mock ValueError"

        r3 = client.get("/api/tags/watch/records")
        assert r3.status_code == 400
        assert r3.json()["detail"] == "mock ValueError"

        r4 = client.post("/api/portfolio", json={"symbol": "AAPL"})
        assert r4.status_code == 400
        assert r4.json()["detail"] == "mock ValueError"


def test_quant_record_detail():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.get_record.return_value = None
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/record/test_cap/detail")
        assert r.status_code == 404

    mock_sup.storage.get_record.return_value = {"capture_id": "test_cap"}
    mock_sup.storage.get_reviews_for_capture.return_value = []
    mock_engine = MagicMock()
    mock_engine.reaction_for.side_effect = Exception("reaction error")
    with patch("catchem.api._SUPERVISOR", mock_sup), patch("catchem.api._QUANT_ENGINE", mock_engine):
        r = client.get("/api/quant/record/test_cap/detail")
        assert r.status_code == 200
        body = r.json()
        assert body["reaction"] is None


def test_quant_endpoints_reports():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.co_occurrence.return_value = None
    mock_engine.anomalies.return_value = None
    mock_engine.spillover.return_value = None
    with patch("catchem.api._QUANT_ENGINE", mock_engine):
        r1 = client.get("/api/quant/co-occurrence")
        assert r1.status_code == 200
        assert r1.json()["total_records"] == 0

        r2 = client.get("/api/quant/anomalies")
        assert r2.status_code == 200
        assert r2.json()["volume_anomalies"] == []

        r3 = client.get("/api/quant/spillover")
        assert r3.status_code == 200
        assert r3.json()["edges"] == []

    from dataclasses import dataclass

    @dataclass
    class DummyCoOccurrence:
        total_records: int
        distinct_assets: int
        distinct_reasons: int
        distinct_symbols: int
        asset_reason_cells: list
        strong_edges: list
        asset_concentration: list

    @dataclass
    class DummyAnomalies:
        bucket_minutes: int
        window_buckets: int
        z_threshold: float
        volume_anomalies: list
        sentiment_shocks: list
        symbol_bursts: list

    @dataclass
    class DummySpillover:
        bucket_minutes: int
        lag_buckets: int
        surge_z_threshold: float
        edges: list
        self_loops: list
        total_buckets: int

    mock_engine.co_occurrence.return_value = DummyCoOccurrence(1, 2, 3, 4, [], [], [])
    mock_engine.anomalies.return_value = DummyAnomalies(30, 12, 2.0, [], [], [])
    mock_engine.spillover.return_value = DummySpillover(30, 1, 1.5, [], [], 10)

    with patch("catchem.api._QUANT_ENGINE", mock_engine):
        r1 = client.get("/api/quant/co-occurrence")
        assert r1.status_code == 200
        assert r1.json()["total_records"] == 1

        r2 = client.get("/api/quant/anomalies")
        assert r2.status_code == 200
        assert r2.json()["volume_anomalies"] == []

        r3 = client.get("/api/quant/spillover")
        assert r3.status_code == 200
        assert r3.json()["total_buckets"] == 10


def test_quant_velocity_market_heatmap():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []

    from dataclasses import dataclass

    @dataclass
    class DummyVelocity:
        current_rate_per_min: float
        ema_fast: float
        ema_slow: float
        baseline_rate: float
        baseline_std: float
        acceleration_z: float
        regime: str
        samples: list

    mock_velocity = DummyVelocity(1.0, 1.1, 1.2, 1.0, 0.5, 0.0, "normal", [])
    mock_heatmap_res = {"heatmap_grid": [], "peak_cells": []}

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.quant.news_velocity.compute_velocity", return_value=mock_velocity),
        patch("catchem.quant.arrival_heatmap.compute_heatmap", return_value=mock_heatmap_res),
    ):
        r1 = client.get("/api/quant/news-velocity")
        assert r1.status_code == 200
        assert r1.json()["current_rate_per_min"] == 1.0

        r2 = client.get("/api/quant/market-time")
        assert r2.status_code == 200
        assert "buckets" in r2.json()

        r3 = client.get("/api/quant/arrival-heatmap")
        assert r3.status_code == 200
        assert r3.json()["peak_cells"] == []


def test_quant_cluster_members():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.clusters.return_value = []
    with patch("catchem.api._QUANT_ENGINE", mock_engine):
        r = client.get("/api/quant/cluster/abc/members")
        assert r.status_code == 404

    from dataclasses import dataclass

    @dataclass
    class DummyCluster:
        cluster_id: str
        capture_ids: list

    mock_cluster = DummyCluster("abc", ["found_cap", "missing_cap"])
    mock_engine.clusters.return_value = [mock_cluster]

    mock_sup = MagicMock()

    def get_rec_side_effect(cap_id):
        if cap_id == "found_cap":
            return {"capture_id": "found_cap", "title": "Found title"}
        return None

    mock_sup.storage.get_record.side_effect = get_rec_side_effect

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/cluster/abc/members")
        assert r.status_code == 200
        body = r.json()
        assert body["returned"] == 1
        assert body["members"][0]["capture_id"] == "found_cap"


def test_quant_heatmap_records():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.by_label.return_value = [
        {"capture_id": "cap1", "impact_reason_codes": ["earnings", "mergers"]},
        {"capture_id": "cap2", "impact_reason_codes": ["other"]},
        {"capture_id": "cap3", "impact_reason_codes": ["earnings"]},
    ]

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/heatmap/records?asset=tech&reason=earnings&limit=1")
        assert r.status_code == 200
        body = r.json()
        assert body["total_returned"] == 1
        assert body["records"][0]["capture_id"] == "cap1"


def test_quant_explain_various():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    mock_sup = MagicMock()

    # Empty kind
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.post("/api/quant/explain", json={"kind": "", "payload": {}})
        assert r.status_code == 422

    # Client None or budget exhausted
    mock_sup.reviewers.deepseek.return_value = None
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.post("/api/quant/explain", json={"kind": "anomaly", "payload": {"symbol": "BTC"}})
        assert r.status_code == 200
        assert r.json()["source"] == "local"

    # DeepSeek succeeds
    mock_ds = MagicMock()
    mock_ds.model = "deepseek-chat"
    mock_ds._base_url = "https://api.deepseek.com"
    mock_ds._api_key = "fake-key"
    mock_ds.estimate_usd.return_value = 0.01
    mock_sup.reviewers.deepseek.return_value = mock_ds
    mock_sup.reviewers.budget_state.return_value.exhausted = False

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "This is a DeepSeek explanation"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    mock_ds._client.post.return_value = mock_response

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.post("/api/quant/explain", json={"kind": "anomaly", "payload": {"symbol": "BTC"}})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "deepseek"
        assert body["narrative"] == "This is a DeepSeek explanation"
        assert body["usd_cost"] == 0.01

    # DeepSeek non-200 response
    mock_response.status_code = 500
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.post("/api/quant/explain", json={"kind": "anomaly", "payload": {"symbol": "BTC"}})
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "http_500"

    # DeepSeek raises Exception
    mock_ds._client.post.side_effect = Exception("network fail")
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.post("/api/quant/explain", json={"kind": "anomaly", "payload": {"symbol": "BTC"}})
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "network fail"


@pytest.mark.asyncio
async def test_live_read_stream_extra_generators():
    app = create_app(Settings())
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {"n_records_window": 10}
    mock_sup = MagicMock()

    mock_client = MagicMock()
    mock_client.model = "deepseek-chat"
    mock_client._base_url = "https://api.deepseek.com"
    mock_client._api_key = "fake-key"
    mock_client.estimate_usd.return_value = 0.05
    mock_sup.reviewers.deepseek.return_value = mock_client
    mock_sup.reviewers.budget_state.return_value.exhausted = False

    # 1. Empty generator
    async def mock_stream_empty(*args, **kwargs):
        if False:
            yield {}

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", side_effect=mock_stream_empty),
    ):
        r = client.get("/api/quant/live-read-stream")
        assert r.status_code == 200
        assert "switching to local synthesis" not in r.text

    # 2. Delta with empty text and done loop back
    async def mock_stream_empty_text(*args, **kwargs):
        yield {"type": "delta", "text": ""}
        yield {"type": "delta", "text": "actual text"}
        yield {"type": "something_else"}
        yield {"type": "done"}

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", side_effect=mock_stream_empty_text),
    ):
        r = client.get("/api/quant/live-read-stream")
        assert r.status_code == 200
        assert "actual text" in r.text

    # 3. Partial generator delta chunks followed by error
    async def mock_stream_partial_error(*args, **kwargs):
        yield {"type": "delta", "text": "Partial chunk"}
        yield {"type": "error", "error": "mid-call connection issue"}

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", side_effect=mock_stream_partial_error),
    ):
        r = client.get("/api/quant/live-read-stream")
        assert r.status_code == 200
        assert "Partial chunk" in r.text
        assert "switching to local synthesis" in r.text


def test_api_reviews_run_on_demand_stub_success():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_record = {
        "capture_id": "glob-1",
        "doc_id": "doc-1",
        "title": "Title",
        "text_excerpt": "Excerpt",
        "language": "en",
        "url": "http://example.com",
        "domain": "example.com",
        "published_ts": "2026-06-03T12:00:00Z",
        "is_finance_relevant": True,
        "finance_relevance_score": 0.8,
        "asset_classes": ["equity"],
        "impact_reason_codes": ["earnings"],
        "candidate_symbols": ["AAPL"],
        "candidate_entities": ["Apple"],
        "impact_horizons": ["short"],
        "sentiment_label": "positive",
        "sentiment_score": 0.8,
        "evidence_sentences": ["evidence"],
        "reason_text": "reason",
        "component_scores": {},
        "diagnostic_multimodal_enabled": False,
        "diagnostic_multimodal_result": None,
    }
    mock_sup.storage.get_record.return_value = mock_record

    # Mock stub properties with real strings to pass Pydantic validation
    mock_stub = MagicMock()
    mock_stub.reviewer_id = "stub_reviewer"
    mock_stub.reviewer_version = "1.0"
    mock_sup.reviewers.stub.return_value = mock_stub

    mock_client = MagicMock()
    mock_client.model = "deepseek-chat"
    mock_client._base_url = "https://api.deepseek.com"
    mock_client._api_key = "fake-key"
    mock_sup.reviewers.deepseek.return_value = mock_client

    mock_sup.reviewers.budget_state.return_value.exhausted = False

    mock_payload = MagicMock()
    mock_payload.error_code = None
    mock_payload.to_storage_row.return_value = {
        "capture_id": "glob-1",
        "reviewer_id": "deepseek",
        "payload": {},
    }
    mock_sup.reviewers.run_and_persist_deepseek.return_value = mock_payload

    with patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.post("/api/reviews/glob-1/run")
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_quant_engine_concurrent_init():
    import catchem.api
    from catchem.api import _get_quant_engine

    catchem.api._QUANT_ENGINE = None

    class MockLock:
        def __enter__(self):
            catchem.api._QUANT_ENGINE = MagicMock()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("catchem.api._SUPERVISOR", MagicMock()), patch("catchem.api._QUANT_ENGINE_LOCK", MockLock()):
        engine = _get_quant_engine()
        assert engine is not None


def test_build_news_poller_no_feeds():
    from catchem.api import _build_news_poller
    from catchem.settings import Settings

    s = Settings()
    s.news.poller_enabled = True
    s.news.feeds = []
    sup = MagicMock()
    poller = _build_news_poller(sup, s)
    assert poller is not None


def test_build_ws_channel_enabled():
    from catchem.api import _build_ws_channel
    from catchem.settings import Settings

    s = Settings()
    s.news.websocket_enabled = True
    sup = MagicMock()
    ws = _build_ws_channel(sup, s)
    assert ws is not None


def test_build_archiver_no_drive_dir():
    from catchem.api import _build_archiver
    from catchem.settings import Settings

    s = Settings()
    s.archive.enabled = True
    s.archive.drive_dir = ""
    sup = MagicMock()
    archiver = _build_archiver(sup, s)
    assert archiver is not None


@pytest.mark.asyncio
async def test_lifespan_startup_failure_all_components():
    from unittest.mock import AsyncMock

    from catchem.api import lifespan
    from catchem.settings import Settings

    s = Settings()
    s.news.poller_enabled = True
    s.news.websocket_enabled = True
    s.archive.enabled = True
    s.archive.drive_dir = "/tmp/drive"

    app = MagicMock()
    app.state.catchem_settings = s

    mock_poller = MagicMock()
    mock_poller.stop = AsyncMock(side_effect=Exception("poller stop fail"))

    mock_ws = MagicMock()
    mock_ws.stop = AsyncMock(side_effect=Exception("ws stop fail"))

    mock_archiver = MagicMock()
    mock_archiver.start.side_effect = Exception("archiver start fail")
    mock_archiver.stop = AsyncMock(side_effect=Exception("archiver stop fail"))

    mock_sup = MagicMock()
    mock_sup.close.side_effect = Exception("sup close fail")

    with (
        patch("catchem.api.Supervisor", return_value=mock_sup),
        patch("catchem.api._build_news_poller", return_value=mock_poller),
        patch("catchem.api._build_ws_channel", return_value=mock_ws),
        patch("catchem.api._build_archiver", return_value=mock_archiver),
    ):
        with pytest.raises(Exception, match="archiver start fail"):
            async with lifespan(app):
                pass


@pytest.mark.asyncio
async def test_lifespan_normal_shutdown_no_supervisor():
    import catchem.api
    from catchem.api import lifespan
    from catchem.settings import Settings

    s = Settings()
    s.news.poller_enabled = False
    s.news.websocket_enabled = False
    s.archive.enabled = False

    app = MagicMock()
    app.state.catchem_settings = s

    async with lifespan(app):
        catchem.api._SUPERVISOR = None


def test_production_safe_endpoints():
    from catchem.settings import CatchemMode, Settings

    s = Settings()
    s.mode = CatchemMode.PRODUCTION_SAFE
    app = create_app(s)
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.status.return_value = {"diagnostic_enabled": True}
    full_rec = {
        "capture_id": "1",
        "doc_id": "doc-1",
        "title": "Secret info",
        "domain": "example.com",
        "language": "en",
        "url": "http://example.com",
        "is_finance_relevant": True,
        "finance_relevance_score": 0.9,
        "asset_classes": ["equity"],
        "impact_reason_codes": ["earnings"],
        "candidate_symbols": ["AAPL"],
        "sentiment_label": "positive",
        "sentiment_score": 0.8,
        "evidence_sentences": ["evidence line"],
        "published_ts": "2026-06-03T12:00:00Z",
        "created_at": "2026-06-03T12:00:00Z",
    }
    mock_sup.storage.recent_records.return_value = [full_rec]

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.api._is_production_safe", return_value=True),
    ):
        r1 = client.get("/metrics")
        assert r1.status_code == 200
        assert r1.json()["diagnostic_enabled"] is False

        r2 = client.get("/dashboard")
        assert r2.status_code == 200

        r3 = client.get("/recent")
        assert r3.status_code == 200


def test_api_reviews_run_on_demand_success_with_stub():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.get_record.return_value = {"capture_id": "glob-1", "doc_id": "doc-1"}

    mock_stub = MagicMock()
    mock_stub.reviewer_id = "stub_reviewer"
    mock_stub.reviewer_version = "1.0"
    mock_sup.reviewers.stub.return_value = mock_stub

    mock_client = MagicMock()
    mock_client.model = "deepseek-chat"
    mock_client._base_url = "https://api.deepseek.com"
    mock_client._api_key = "fake-key"
    mock_sup.reviewers.deepseek.return_value = mock_client

    mock_sup.reviewers.budget_state.return_value.exhausted = False

    mock_payload = MagicMock()
    mock_payload.error_code = None
    mock_payload.to_storage_row.return_value = {
        "capture_id": "glob-1",
        "reviewer_id": "deepseek",
        "payload": {},
    }
    mock_sup.reviewers.run_and_persist_deepseek.return_value = mock_payload

    mock_rec_instance = MagicMock()
    mock_stub_payload = MagicMock()
    mock_stub_payload.to_storage_row.return_value = {
        "capture_id": "glob-1",
        "reviewer_id": "stub_reviewer",
        "payload": {},
    }

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.schemas.FinancialImpactRecord", return_value=mock_rec_instance),
        patch("catchem.reviewers.record_to_review_payload", return_value=mock_stub_payload),
    ):
        response = client.post("/api/reviews/glob-1/run")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        mock_sup.storage.upsert_review.assert_called_with(mock_stub_payload.to_storage_row())


@pytest.mark.asyncio
async def test_live_read_stream_normal_exhaustion():
    app = create_app(Settings())
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {"n_records_window": 10}
    mock_sup = MagicMock()

    mock_client = MagicMock()
    mock_client.model = "deepseek-chat"
    mock_client._base_url = "https://api.deepseek.com"
    mock_client._api_key = "fake-key"
    mock_client.estimate_usd.return_value = 0.05
    mock_sup.reviewers.deepseek.return_value = mock_client
    mock_sup.reviewers.budget_state.return_value.exhausted = False

    async def mock_stream_normal(*args, **kwargs):
        yield {"type": "delta", "text": "normal chunk"}

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", side_effect=mock_stream_normal),
    ):
        r = client.get("/api/quant/live-read-stream")
        assert r.status_code == 200
        assert "normal chunk" in r.text


def test_quant_symbol_correlation():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []

    from dataclasses import dataclass

    @dataclass
    class DummyPair:
        symbol_a: str
        symbol_b: str
        pearson_r: float
        n_buckets: int
        a_total: int
        b_total: int

    mock_pairs = [DummyPair("AAPL", "MSFT", 0.85, 10, 5, 6)]

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.quant.symbol_correlation.compute_pairs", return_value=mock_pairs),
    ):
        r = client.get("/api/quant/symbol-correlation")
        assert r.status_code == 200
        body = r.json()
        assert body["pairs"][0]["symbol_a"] == "AAPL"


def test_quant_heatmap_records_more():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.by_label.return_value = [
        {"capture_id": "cap1", "impact_reason_codes": ["earnings", "mergers"]},
        {"capture_id": "cap2", "impact_reason_codes": ["other"]},
        {"capture_id": "cap3", "impact_reason_codes": ["earnings"]},
    ]

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/heatmap/records?asset=tech&reason=earnings&limit=5")
        assert r.status_code == 200
        body = r.json()
        assert body["total_returned"] == 2


def test_search_palette_empty_query():
    from catchem.api import _rate_limit_search

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_search] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/search?q=  ")
        assert r.status_code == 422
        assert r.json()["detail"] == "q must be non-empty"


def test_search_palette_symbols_break_limit():
    from catchem.api import _rate_limit_search

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_search] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = [
        {"candidate_symbols": ["AAPL", "AAPL", "AAPL"]},
    ]
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/search?q=AAPL&limit=1")
        assert r.status_code == 200
        body = r.json()
        assert len(body["symbols"]) == 1


def test_search_palette_clusters_exception():
    from catchem.api import _rate_limit_search

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_search] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.api._get_quant_engine", side_effect=Exception("clusters fail")),
    ):
        r = client.get("/api/search?q=abc")
        assert r.status_code == 200
        assert r.json()["clusters"] == []


def test_search_palette_clusters_matching():
    from catchem.api import _rate_limit_search

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_search] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []

    mock_engine = MagicMock()
    from dataclasses import dataclass

    @dataclass
    class DummyCluster:
        cluster_id: str
        size: int
        dominant_symbols: list

    mock_engine.clusters.return_value = [
        DummyCluster("cluster_1", 2, ["AAPL"]),
        DummyCluster("cluster_2", 3, ["AAPL"]),
    ]

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.api._get_quant_engine", return_value=mock_engine),
    ):
        r = client.get("/api/search?q=AAPL&limit=1")
        assert r.status_code == 200
        body = r.json()
        assert len(body["clusters"]) == 1


def test_export_records_with_filters():
    from catchem.api import _rate_limit_db_export

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_db_export] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = [
        {"capture_id": "1", "asset_classes": ["tech"], "impact_reason_codes": ["earnings"]},
        {"capture_id": "2", "asset_classes": ["energy"], "impact_reason_codes": ["macro"]},
    ]
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/export/records?format=json&asset_class=tech&reason_code=earnings")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1


def test_export_records_csv_non_standard_fields():
    from catchem.api import _rate_limit_db_export

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_db_export] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = [
        {
            "capture_id": "1",
            "asset_classes": None,
            "title": None,
            "impact_reason_codes": ["earnings"],
            "candidate_symbols": ["AAPL"],
        }
    ]
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/export/records?format=csv")
        assert r.status_code == 200
        assert "earnings" in r.text


def test_export_reviews_with_filters():
    from catchem.api import _rate_limit_db_export

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_db_export] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()

    stub_row = {
        "capture_id": "1",
        "payload": {
            "asset_classes": ["tech"],
            "impact_reason_codes": ["earnings"],
            "candidate_symbols": ["AAPL"],
            "finance_relevance_score": 0.8,
        },
    }
    ds_row = {
        "capture_id": "1",
        "payload": {},
    }
    mock_sup.storage.reviews_with_pair.return_value = [(stub_row, ds_row)]

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get(
            "/api/export/reviews?format=json&asset_class=tech&reason_code=earnings&symbol=AAPL&min_score=0.5"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1


def test_db_info_not_exists():
    app = create_app(Settings())
    client = TestClient(app)
    with patch("pathlib.Path.exists", return_value=False):
        r = client.get("/api/db/info")
        assert r.status_code == 200
        assert r.json()["exists"] is False


def test_db_stats_table_error():
    from catchem.api import _rate_limit_db_export

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_db_export] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.side_effect = [
        [("records",)],
        [("index_1", "records")],
    ]
    mock_conn.execute.return_value.fetchone.side_effect = [
        Exception("query error"),
        (100,),
        (4096,),
    ]
    mock_sup.storage._connection.return_value.__enter__.return_value = mock_conn
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/db/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["tables"][0]["rows"] == -1


def test_db_import_exceeds_max_bytes():
    from catchem.api import _rate_limit_db_import, create_app
    from catchem.settings import Settings

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_db_import] = lambda: None
    client = TestClient(app)

    class FakeLargeChunk:
        def startswith(self, prefix):
            return True

        def __len__(self):
            return 201 * 1024 * 1024

    async def mock_read(*args, **kwargs):
        return FakeLargeChunk()

    with patch("starlette.datastructures.UploadFile.read", mock_read):
        r = client.post("/api/db/import", files={"file": ("db.sqlite", b"SQLite format 3\x00")})
        assert r.status_code == 413
        assert r.json()["detail"] == "upload exceeds 200 MB cap"


def test_db_import_exceeds_custom_configured_max_bytes():
    from unittest.mock import patch

    import catchem.api
    from catchem.api import _rate_limit_db_import, create_app
    from catchem.settings import Settings

    # Test custom limit of 500 bytes (does not divide cleanly by MB)
    s = Settings()
    s.api.max_import_size_bytes = 500
    app = create_app(s)
    app.dependency_overrides[_rate_limit_db_import] = lambda: None
    client = TestClient(app)

    class FakeChunk:
        def startswith(self, prefix):
            return True

        def __len__(self):
            return 501

    async def mock_read(*args, **kwargs):
        return FakeChunk()

    old_settings = catchem.api._SETTINGS
    catchem.api._SETTINGS = s
    try:
        with patch("starlette.datastructures.UploadFile.read", mock_read):
            r = client.post("/api/db/import", files={"file": ("db.sqlite", b"SQLite format 3\x00")})
            assert r.status_code == 413
            assert r.json()["detail"] == "upload exceeds 500 bytes cap"
    finally:
        catchem.api._SETTINGS = old_settings

    # Test custom limit of 2 MB (divides cleanly by MB)
    s2 = Settings()
    s2.api.max_import_size_bytes = 2 * 1024 * 1024
    app2 = create_app(s2)
    app2.dependency_overrides[_rate_limit_db_import] = lambda: None
    client2 = TestClient(app2)

    class FakeLargeChunk2:
        def startswith(self, prefix):
            return True

        def __len__(self):
            return 3 * 1024 * 1024

    async def mock_read2(*args, **kwargs):
        return FakeLargeChunk2()

    old_settings = catchem.api._SETTINGS
    catchem.api._SETTINGS = s2
    try:
        with patch("starlette.datastructures.UploadFile.read", mock_read2):
            r = client2.post("/api/db/import", files={"file": ("db.sqlite", b"SQLite format 3\x00")})
            assert r.status_code == 413
            assert r.json()["detail"] == "upload exceeds 2 MB cap"
    finally:
        catchem.api._SETTINGS = old_settings


def test_ui_archive_status_disabled():
    app = create_app(Settings())
    client = TestClient(app)
    with patch("catchem.api._ARCHIVER", None):
        r = client.get("/ui/archive-status")
        assert r.status_code == 200
        assert r.json()["enabled"] is False


def test_ui_archive_now_disabled():
    app = create_app(Settings())
    client = TestClient(app)
    with patch("catchem.api._ARCHIVER", None):
        r = client.post("/ui/archive-now")
        assert r.status_code == 503
        assert r.json()["detail"] == "archiver_disabled"


@pytest.mark.asyncio
async def test_ui_stream():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.count_records.return_value = {"total": 5, "dlq": 0}
    mock_sup.storage.dlq_count.return_value = 0

    from unittest.mock import AsyncMock

    is_disconnected_mock = AsyncMock()
    is_disconnected_mock.side_effect = [False, False, True]

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("starlette.requests.Request.is_disconnected", is_disconnected_mock),
        patch("asyncio.sleep", return_value=None),
    ):
        with client.stream("GET", "/ui/stream") as r:
            assert r.status_code == 200
            lines = []
            for line in r.iter_lines():
                lines.append(line)
                if len(lines) >= 6:
                    break

            text = "\n".join(lines)
            assert "event: summary" in text
            assert "event: tick" in text


def test_spa_fallbacks():
    app = create_app(Settings())
    client = TestClient(app)

    with patch("catchem.api._render_spa_with_nonce", return_value=(None, None)):
        r = client.get("/replay")
        assert r.status_code == 200
        assert "catchem" in r.text

    with patch("catchem.api._render_spa_with_nonce", return_value=("<html>rendered spa</html>", "nonce-123")):
        r = client.get("/replay")
        assert r.status_code == 200
        assert "rendered spa" in r.text
        assert "Content-Security-Policy" in r.headers

    with patch("catchem.api._render_spa_with_nonce", return_value=(None, None)):
        r = client.get("/some-custom-spa-route")
        assert r.status_code == 200
        assert "catchem" in r.text


def test_ui_guards_unexpected_error():
    app = create_app(Settings())
    client = TestClient(app)
    with patch("catchem.api.snapshot_guard_state", side_effect=Exception("unexpected error")):
        r = client.get("/ui/guards")
        assert r.status_code == 200
        assert r.json()["ok"] is False


@pytest.mark.asyncio
async def test_lifespan_startup_failure_supervisor_fails():
    from unittest.mock import MagicMock, patch

    from catchem.api import lifespan
    from catchem.settings import Settings

    s = Settings()
    app = MagicMock()
    app.state.catchem_settings = s

    with patch("catchem.api.Supervisor", side_effect=Exception("supervisor failed")):
        with pytest.raises(Exception, match="supervisor failed"):
            async with lifespan(app):
                pass


def test_production_safe_disabled_endpoints():
    from unittest.mock import MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.status.return_value = {"diagnostic_enabled": True}
    mock_sup.storage.recent_records.return_value = []

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.api._is_production_safe", return_value=False),
    ):
        r1 = client.get("/metrics")
        assert r1.status_code == 200
        assert r1.json()["diagnostic_enabled"] is True

        r2 = client.get("/dashboard")
        assert r2.status_code == 200


def test_api_stats_no_registry():
    from unittest.mock import MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage._connection.return_value.__enter__.return_value = MagicMock()
    if hasattr(mock_sup, "reviewers"):
        del mock_sup.reviewers

    from catchem.api import _STATS_CACHE

    _STATS_CACHE.clear()

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/stats")
        assert r.status_code == 200
        assert r.json()["reviewers"]["deepseek_usd_spent"] == 0.0


def test_search_palette_clusters_more_branches():
    from unittest.mock import MagicMock, patch

    from catchem.api import _rate_limit_search

    app = create_app(Settings())
    app.dependency_overrides[_rate_limit_search] = lambda: None
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []

    mock_engine = MagicMock()
    from dataclasses import dataclass

    @dataclass
    class DummyCluster:
        cluster_id: str
        size: int
        dominant_symbols: list

    mock_engine.clusters.return_value = [
        DummyCluster("other", 2, ["MSFT"]),
        DummyCluster("aapl_cluster", 3, ["AAPL"]),
    ]

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.api._get_quant_engine", return_value=mock_engine),
    ):
        r = client.get("/api/search?q=AAPL&limit=2")
        assert r.status_code == 200
        body = r.json()
        assert len(body["clusters"]) == 1


def test_db_import_multi_chunk():
    from unittest.mock import patch

    from catchem.api import _rate_limit_db_import
    from catchem.settings import Settings

    s = Settings()
    app = create_app(s)
    app.dependency_overrides[_rate_limit_db_import] = lambda: None
    client = TestClient(app)

    chunks = [b"SQLite format 3\x00", b"some extra data", b""]

    async def mock_read(*args, **kwargs):
        if chunks:
            return chunks.pop(0)
        return b""

    with (
        patch("starlette.datastructures.UploadFile.read", mock_read),
        patch("pathlib.Path.replace"),
        patch("pathlib.Path.unlink"),
        patch("os.fsync"),
        patch("shutil.copy2"),
    ):
        r = client.post("/api/db/import", files={"file": ("db.sqlite", b"SQLite format 3\x00")})
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_log_tail_file_exists(tmp_path):
    s = Settings()
    s.paths.catchem_output_dir = tmp_path
    s.logging.file = "data/logs/catchem.log"

    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "catchem.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

    app = create_app(s)
    client = TestClient(app)

    with patch("catchem.api._SETTINGS", s), patch("catchem.api.load_settings", return_value=s):
        r = client.get("/ui/log-tail?lines=2")
        assert r.status_code == 200
        body = r.json()
        assert body["lines"] == ["line2", "line3"]
        assert body["total_lines"] == 3
        assert body["truncated"] is True

    s2 = Settings()
    s2.paths.catchem_output_dir = tmp_path
    s2.logging.file = str(log_file)

    app2 = create_app(s2)
    client2 = TestClient(app2)
    with patch("catchem.api._SETTINGS", s2), patch("catchem.api.load_settings", return_value=s2):
        r2 = client2.get("/ui/log-tail?lines=2")
        assert r2.status_code == 200
        assert r2.json()["total_lines"] == 3


def test_ui_facets_and_timeline_missing_fields():
    from unittest.mock import MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_sup = MagicMock()

    mock_sup.storage.recent_records.return_value = [
        {
            "is_finance_relevant": True,
            "domain": "bloomberg.com",
            "sentiment_label": "positive",
            "published_ts": "2026-06-03T12:00:00Z",
            "asset_classes": ["equity"],
            "impact_reason_codes": ["earnings"],
            "candidate_symbols": ["AAPL"],
        },
        {
            "is_finance_relevant": False,
            "domain": None,
            "sentiment_label": None,
            "created_at": "2026-06-03T13:00:00+00:00",
            "asset_classes": [],
            "impact_reason_codes": [],
            "candidate_symbols": [],
        },
        {
            "is_finance_relevant": False,
            "domain": None,
            "sentiment_label": None,
        },
        {
            "is_finance_relevant": False,
            "published_ts": "invalid-date-format",
        },
        {
            "is_finance_relevant": False,
            "published_ts": "2026-06-03T14:00:00",
        },
    ]

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r_facets = client.get("/ui/facets")
        assert r_facets.status_code == 200
        body_facets = r_facets.json()
        assert body_facets["window_total"] == 5
        assert body_facets["window_relevant"] == 1

        r_timeline = client.get("/ui/timeline")
        assert r_timeline.status_code == 200
        body_timeline = r_timeline.json()
        assert len(body_timeline["series"]) == 3


def test_ui_trends():
    from unittest.mock import MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = [
        {
            "published_ts": "2026-06-03T12:00:00Z",
            "asset_classes": ["equity"],
        },
        {
            "created_at": "2026-06-03T13:00:00Z",
            "asset_classes": ["crypto"],
        },
        {
            "published_ts": None,
            "created_at": None,
        },
        {
            "published_ts": "invalid-ts",
        },
    ]

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/ui/trends")
        assert r.status_code == 200
        body = r.json()
        assert len(body["buckets"]) == 2
        assert "equity" in body["asset_classes"]


def test_ui_benchmark_history_file_exists(tmp_path):
    from catchem.settings import Settings

    s = Settings()
    s.paths.catchem_output_dir = tmp_path

    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    history_file = results_dir / "benchmark_history.jsonl"
    history_file.write_text(
        '{"schema_version": 1, "dataset_name": "t", "generated_at": "2026-06-03", "relevance": {}, "n": 10}\n\n{"invalid json\n',
        encoding="utf-8",
    )

    app = create_app(s)
    client = TestClient(app)

    with patch("catchem.api._SETTINGS", s), patch("catchem.api.load_settings", return_value=s):
        r = client.get("/ui/benchmark/history")
        assert r.status_code == 200
        body = r.json()
        assert len(body["history"]) == 1
        assert body["history"][0]["n"] == 10


def test_ui_symbol_missing_sentiment():
    from unittest.mock import MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_sup.storage.by_label.return_value = [
        {
            "capture_id": "cap-1",
            "doc_id": "doc-1",
            "title": "A Title",
            "domain": "google.com",
            "impact_reason_codes": ["earnings"],
            "sentiment_label": None,
        }
    ]

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/ui/symbol/AAPL")
        assert r.status_code == 200
        body = r.json()
        assert body["sentiment_distribution"] == {}


def test_api_symbol_sentiment_trend_out_of_bounds():
    from unittest.mock import MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_sup = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [
        {"day": "2026-06-10", "label": "extra_label", "cnt": 5}
    ]
    mock_sup.storage._connection.return_value.__enter__.return_value = mock_conn

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/symbols/AAPL/sentiment-trend?days=1")
        assert r.status_code == 200
        body = r.json()
        for s in body["series"]:
            assert s["positive"] == 0


def test_ui_news_status_and_poll_now_and_probe_error():
    from unittest.mock import AsyncMock, MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_poller = MagicMock()
    mock_poller.feeds = []
    mock_poller.interval_seconds = 60
    mock_poller.last_run_at = None
    mock_poller.next_run_at = None
    mock_poller.feed_health_snapshot.return_value = [
        {"name": "Feed 1", "url": "http://feed1", "backed_off": True, "ok": True}
    ]
    mock_poller.poll_now = AsyncMock(return_value=1)
    mock_poller.last_error = None
    mock_poller.last_new_at = None
    mock_poller.max_item_age_seconds = None
    mock_poller.last_stale_skipped = 0
    mock_poller.last_avg_publisher_lag_seconds = None
    mock_poller.last_median_publisher_lag_seconds = None
    mock_poller.empty_ticks = 0
    mock_poller.total_ingested = 10
    mock_poller.last_ingested = 5
    mock_poller.is_polling = False

    with patch("catchem.api._NEWS_POLLER", mock_poller):
        r = client.get("/ui/news-status")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        r_poll = client.post("/ui/news-poll-now")
        assert r_poll.status_code == 200
        assert r_poll.json()["ingested"] == 1

        r_sources = client.get("/api/news/sources")
        assert r_sources.status_code == 200
        assert r_sources.json()["sources"][0]["last_status"] == "backed_off"

    with patch("catchem.api._NEWS_POLLER", None):
        r_poll_disabled = client.post("/ui/news-poll-now")
        assert r_poll_disabled.status_code == 503

    mock_poller_probe = MagicMock()
    mock_spec = MagicMock()
    mock_spec.url = "http://feed1"
    mock_poller_probe.feeds = [mock_spec]
    mock_poller_probe.probe_feed_async = AsyncMock(side_effect=Exception("probe error"))
    with patch("catchem.api._NEWS_POLLER", mock_poller_probe):
        r_probe = client.post("/api/news/sources/probe", json={"url": "http://feed1"})
        assert r_probe.status_code == 200
        assert r_probe.json()["ok"] is False
        assert r_probe.json()["error"] == "probe error"


def test_global_tone_cache_hit():
    import time

    from catchem.api import _GLOBAL_TONE_CACHE

    app = create_app(Settings())
    client = TestClient(app)

    _GLOBAL_TONE_CACHE["payload"] = {"cached": "data"}
    _GLOBAL_TONE_CACHE["expires_at"] = time.monotonic() + 100

    r = client.get("/api/quant/global-tone")
    assert r.status_code == 200
    assert r.json() == {"cached": "data"}


def test_ui_archive_now_active():
    from unittest.mock import AsyncMock, MagicMock, patch

    app = create_app(Settings())
    client = TestClient(app)
    mock_archiver = MagicMock()
    from dataclasses import dataclass

    @dataclass
    class DummyResult:
        archived: int
        csv_path: str
        error: str

    mock_archiver.archive_now = AsyncMock(return_value=DummyResult(5, "/path/to.csv", None))
    mock_archiver.total_archived = 10

    with patch("catchem.api._ARCHIVER", mock_archiver):
        r = client.post("/ui/archive-now")
        assert r.status_code == 200
        body = r.json()
        assert body["archived"] == 5
        assert body["total_archived"] == 10


@pytest.mark.asyncio
async def test_replay_spa_directly():
    from unittest.mock import patch

    app = create_app(Settings())
    route = next(r for r in app.routes if r.path == "/replay" and "GET" in r.methods)
    with patch("catchem.api._render_spa_with_nonce", return_value=(None, None)):
        r = await route.endpoint()
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_lifespan_startup_failure_various_gaps():
    from unittest.mock import MagicMock, patch

    from catchem.api import lifespan
    from catchem.settings import Settings

    app = MagicMock()
    app.state.catchem_settings = Settings()

    with patch("catchem.api._build_news_poller", side_effect=ValueError("poller build error")):
        with patch("catchem.api.Supervisor", return_value=None):
            with pytest.raises(ValueError, match="poller build error"):
                async with lifespan(app):
                    pass


@pytest.mark.asyncio
async def test_live_read_stream_deepseek_all_branches():
    import json
    from unittest.mock import MagicMock, patch

    from catchem.api import create_app
    from catchem.settings import Settings

    app = create_app(Settings())
    route = next(r for r in app.routes if r.path == "/api/quant/live-read-stream" and "GET" in r.methods)

    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {"n_records_window": 10}
    mock_sup = MagicMock()

    mock_client = MagicMock()
    mock_client.model = "deepseek-chat"
    mock_client._base_url = "https://api.deepseek.com"
    mock_client._api_key = "fake-key"
    mock_client.estimate_usd.return_value = 0.05
    mock_sup.reviewers.deepseek.return_value = mock_client
    mock_sup.reviewers.budget_state.return_value.exhausted = False

    # Branch 1: empty stream (loop body not entered)
    async def mock_stream_empty(*args, **kwargs):
        if False:
            yield {}

    mock_stream_1 = MagicMock(side_effect=mock_stream_empty)

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", mock_stream_1),
    ):
        response = await route.endpoint(limit=1000)
        events = []
        async for item in response.body_iterator:
            events.append(item)

        mock_stream_1.assert_called_once()
        assert any(e.get("event") == "start" for e in events)
        done_event = next(e for e in events if e.get("event") == "done")
        done_data = json.loads(done_event["data"])
        assert done_data["source"] == "local"
        assert done_data["fallback_reason"] == "no_content"

    # Branch 2: non-empty stream (loop body entered, exits via break)
    async def mock_stream_yields(*args, **kwargs):
        yield {"type": "delta", "text": "streamed chunk"}
        yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        yield {"type": "done"}

    mock_stream_2 = MagicMock(side_effect=mock_stream_yields)

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", mock_stream_2),
    ):
        response = await route.endpoint(limit=1000)
        events = []
        async for item in response.body_iterator:
            events.append(item)

        mock_stream_2.assert_called_once()
        assert any(e.get("event") == "start" for e in events)
        chunks = [json.loads(e["data"])["text"] for e in events if e.get("event") == "chunk"]
        assert "streamed chunk" in chunks
        done_event2 = next(e for e in events if e.get("event") == "done")
        done_data2 = json.loads(done_event2["data"])
        assert done_data2["source"] == "deepseek"
        assert done_data2.get("fallback_reason") is None

    # Branch 3: yields delta chunk and finishes naturally (no done/error event)
    async def mock_stream_natural_finish(*args, **kwargs):
        yield {"type": "delta", "text": "natural finish chunk"}

    mock_stream_3 = MagicMock(side_effect=mock_stream_natural_finish)

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", mock_stream_3),
    ):
        response = await route.endpoint(limit=1000)
        events = []
        async for item in response.body_iterator:
            events.append(item)

        mock_stream_3.assert_called_once()
        assert any(e.get("event") == "start" for e in events)
        chunks3 = [json.loads(e["data"])["text"] for e in events if e.get("event") == "chunk"]
        assert "natural finish chunk" in chunks3
        done_event3 = next(e for e in events if e.get("event") == "done")
        done_data3 = json.loads(done_event3["data"])
        assert done_data3["source"] == "deepseek"
        assert done_data3.get("fallback_reason") is None

    # Branch 4: exception raised during iteration
    async def mock_stream_raises(*args, **kwargs):

        yield {"type": "delta", "text": "partial chunk"}
        raise ValueError("stream connection lost")

    mock_stream_4 = MagicMock(side_effect=mock_stream_raises)

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", mock_stream_4),
    ):
        response = await route.endpoint(limit=1000)
        events = []
        async for item in response.body_iterator:
            events.append(item)

        mock_stream_4.assert_called_once()
        assert any(e.get("event") == "start" for e in events)
        # Check that we handled the exception correctly
        done_event4 = next(e for e in events if e.get("event") == "done")
        done_data4 = json.loads(done_event4["data"])
        assert done_data4["source"] == "local"
        assert "stream connection lost" in done_data4["fallback_reason"]


def test_ui_log_tail_os_error(tmp_path):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from catchem.api import create_app
    from catchem.settings import Settings

    s = Settings()
    s.paths.catchem_output_dir = tmp_path
    s.logging.file = "data/logs/catchem.log"
    log_file = tmp_path / "logs" / "catchem.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("line1\nline2\n", encoding="utf-8")

    app = create_app(s)
    client = TestClient(app)

    with (
        patch("catchem.api._SETTINGS", s),
        patch("catchem.api.load_settings", return_value=s),
        patch("pathlib.Path.read_text", side_effect=OSError("Permission denied")),
    ):
        r = client.get("/ui/log-tail?lines=2")
        assert r.status_code == 200
        assert r.json()["lines"] == []


@pytest.mark.asyncio
async def test_replay_spa_directly_with_html():
    from unittest.mock import patch

    from catchem.api import create_app
    from catchem.settings import Settings

    app = create_app(Settings())
    route = next(r for r in app.routes if r.path == "/replay" and "GET" in r.methods)
    with patch("catchem.api._render_spa_with_nonce", return_value=("<html>fake</html>", "fake-nonce")):
        r = await route.endpoint()
        assert r.status_code == 200
        assert r.body == b"<html>fake</html>"
        assert "Content-Security-Policy" in r.headers
        assert "fake-nonce" in r.headers["Content-Security-Policy"]
