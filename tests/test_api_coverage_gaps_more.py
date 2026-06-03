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
        "n_records_window": "invalid_int", # this will trigger TypeError/ValueError in int() cast
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
