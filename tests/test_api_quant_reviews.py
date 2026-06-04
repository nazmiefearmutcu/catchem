from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import Settings


@dataclass
class MockCluster:
    cluster_id: str
    size: int
    coherence: float
    symbols: list[str]
    reasons: list[str]
    domains: list[str]


@dataclass
class MockLeaderboard:
    window_days: int
    total_records: int
    total_domains: int
    sources: list[dict]


@dataclass
class MockNovelty:
    capture_id: str
    novelty_score: float


@dataclass
class MockLeadLag:
    total_events: int
    total_sources: int
    per_event: list[dict]
    per_source: list[dict]


@dataclass
class MockRegime:
    bucket_minutes: int
    shift_threshold: float
    buckets: list[dict]
    detected_shifts: list[dict]


@dataclass
class MockReaction:
    capture_id: str
    reaction_score: float


@dataclass
class MockSentimentMomentum:
    bucket_minutes: int
    min_mentions: int
    tickers: list[dict]


def test_api_reviews_run_on_demand_success():
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
        assert response.json()["capture_id"] == "glob-1"


def test_api_reviews_run_on_demand_errors():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    # 1. 404 Not Found
    mock_sup.storage.get_record.return_value = None
    with patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.post("/api/reviews/glob-1/run")
        assert response.status_code == 404

    # 2. DeepSeek Disabled (503)
    mock_sup.storage.get_record.return_value = {
        "capture_id": "glob-1",
        "doc_id": "doc-1",
        "text_excerpt": "Excerpt",
        "is_finance_relevant": True,
        "finance_relevance_score": 0.5,
    }
    mock_sup.reviewers.deepseek.return_value = None
    with patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.post("/api/reviews/glob-1/run")
        assert response.status_code == 503

    # 3. Budget Exhausted (402)
    mock_client = MagicMock()
    mock_sup.reviewers.deepseek.return_value = mock_client
    mock_sup.reviewers.budget_state.return_value.exhausted = True
    with patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.post("/api/reviews/glob-1/run")
        assert response.status_code == 402


def test_api_reviews_compare():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.reviews_with_pair.return_value = [
        (
            {
                "capture_id": "glob-1",
                "payload": {
                    "is_finance_relevant": True,
                    "finance_relevance_score": 0.8,
                    "sentiment_label": "positive",
                    "asset_classes": ["equity"],
                    "impact_reason_codes": ["earnings"],
                    "candidate_symbols": ["AAPL"],
                },
            },
            {
                "capture_id": "glob-1",
                "payload": {
                    "is_finance_relevant": True,
                    "finance_relevance_score": 0.8,
                    "sentiment_label": "positive",
                    "asset_classes": ["equity"],
                    "impact_reason_codes": ["earnings"],
                    "candidate_symbols": ["AAPL"],
                },
            },
        )
    ]
    mock_sup.storage.get_record.return_value = {"title": "Title", "domain": "domain", "url": "url"}

    with patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.get("/api/reviews/compare")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["summary"]["n"] == 1
        assert data["summary"]["relevance_match_rate"] == 1.0


def test_api_reviews_compare_empty():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.storage.reviews_with_pair.return_value = []

    with patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.get("/api/reviews/compare")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 0
        assert data["summary"]["n"] == 0


def test_api_reviews_patch_settings():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    cfg = mock_sup.settings.reviewers.deepseek
    mock_sup.reviewers.status.return_value = {"enabled": True}

    with patch("catchem.api._SUPERVISOR", mock_sup):
        # 1. Valid patch
        response = client.patch(
            "/api/reviews/settings",
            json={
                "enabled": True,
                "sampling_rate": 0.5,
                "usd_cap": 10.0,
                "api_key": "new-key",
                "model": "new-model",
                "base_url": "new-url",
            },
        )
        assert response.status_code == 200
        assert cfg.enabled is True
        assert cfg.sampling_rate == 0.5
        assert cfg.usd_cap == 10.0
        assert cfg.api_key == "new-key"
        assert cfg.model == "new-model"
        assert cfg.base_url == "new-url"

        # 2. Invalid sampling_rate
        response = client.patch("/api/reviews/settings", json={"sampling_rate": "invalid"})
        assert response.status_code == 422

        # 3. Invalid usd_cap
        response = client.patch("/api/reviews/settings", json={"usd_cap": "invalid"})
        assert response.status_code == 422


def test_api_backtest():
    app = create_app(Settings())
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_result = MagicMock()
    mock_result.schema_version = 1
    mock_result.summary = {"accuracy": 0.9}
    mock_result.calibration_bins = []
    mock_result.relevance_predictions = []

    with (
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.backtest.run_backtest", return_value=mock_result),
    ):
        response = client.get("/api/backtest")
        assert response.status_code == 200
        data = response.json()
        assert data["schema_version"] == 1
        assert data["summary"]["accuracy"] == 0.9


def test_api_webhook_config_get_post():
    app = create_app(Settings())
    client = TestClient(app)

    # post config and check validations
    mock_sup = MagicMock()
    mock_sup.webhook_stats_snapshot.return_value = {"attempted": 1}
    mock_sup.webhook_last_status = "ok"
    mock_sup.webhook_last_error = None

    settings = Settings()
    settings.webhook.enabled = True
    settings.webhook.url = "http://example.com"
    settings.webhook.min_score = 0.5
    settings.webhook.asset_class_filter = ["equity"]
    settings.webhook.reason_code_filter = ["earnings"]
    settings.webhook.timeout_seconds = 10.0

    with patch("catchem.api._SUPERVISOR", mock_sup), patch("catchem.api._SETTINGS", settings):
        # GET config
        response = client.get("/api/webhook/config")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["url_configured"] is True
        assert data["min_score"] == 0.5

        # POST valid config
        response = client.post(
            "/api/webhook/config",
            json={
                "enabled": False,
                "url": "https://example.com/webhook",
                "min_score": 0.8,
                "asset_class_filter": ["crypto"],
                "reason_code_filter": ["macro"],
                "timeout_seconds": 15.0,
            },
        )
        assert response.status_code == 200
        assert settings.webhook.enabled is False
        assert settings.webhook.url == "https://example.com/webhook"
        assert settings.webhook.min_score == 0.8
        assert settings.webhook.asset_class_filter == ["crypto"]
        assert settings.webhook.reason_code_filter == ["macro"]
        assert settings.webhook.timeout_seconds == 15.0

        # POST invalid url
        response = client.post("/api/webhook/config", json={"url": "invalid-url"})
        assert response.status_code == 422

        # POST invalid min_score
        response = client.post("/api/webhook/config", json={"min_score": "invalid"})
        assert response.status_code == 422

        # POST invalid asset_class_filter
        response = client.post("/api/webhook/config", json={"asset_class_filter": "not-a-list"})
        assert response.status_code == 422

        # POST invalid reason_code_filter
        response = client.post("/api/webhook/config", json={"reason_code_filter": "not-a-list"})
        assert response.status_code == 422

        # POST invalid timeout_seconds
        response = client.post("/api/webhook/config", json={"timeout_seconds": "invalid"})
        assert response.status_code == 422

        # POST empty values
        response = client.post(
            "/api/webhook/config",
            json={
                "url": "",
                "asset_class_filter": None,
                "reason_code_filter": None,
            },
        )
        assert response.status_code == 200
        assert settings.webhook.url == ""
        assert settings.webhook.asset_class_filter is None
        assert settings.webhook.reason_code_filter is None


def test_api_quant_endpoints():
    app = create_app(Settings())
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {"n_records_window": 100}
    mock_engine.clusters.return_value = [
        MockCluster(
            cluster_id="c1",
            size=5,
            coherence=0.9,
            symbols=["AAPL"],
            reasons=["earnings"],
            domains=["bloomberg.com"],
        )
    ]
    mock_engine.source_leaderboard.side_effect = [
        MockLeaderboard(window_days=30, total_records=10, total_domains=2, sources=[]),
        None,
    ]
    mock_engine.novelty_timeline.return_value = [MockNovelty(capture_id="cap-1", novelty_score=0.95)]
    mock_engine.novelty_for.side_effect = [MockNovelty(capture_id="cap-1", novelty_score=0.95), None]
    mock_engine.lead_lag.side_effect = [
        MockLeadLag(total_events=5, total_sources=2, per_event=[], per_source=[]),
        None,
    ]
    mock_engine.regime.side_effect = [
        MockRegime(bucket_minutes=60, shift_threshold=0.4, buckets=[], detected_shifts=[]),
        None,
    ]
    mock_engine.reaction_for.side_effect = [MockReaction(capture_id="cap-1", reaction_score=0.8), None]
    mock_engine.sentiment_momentum.side_effect = [
        MockSentimentMomentum(bucket_minutes=240, min_mentions=4, tickers=[]),
        None,
    ]

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", MagicMock()):
        # dashboard
        r = client.get("/api/quant/dashboard")
        assert r.status_code == 200
        assert r.json()["n_records_window"] == 100

        # clusters
        r = client.get("/api/quant/clusters")
        assert r.status_code == 200
        assert r.json()["total"] == 1

        # sources with lb
        r = client.get("/api/quant/sources")
        assert r.status_code == 200
        assert r.json()["total_records"] == 10

        # sources without lb
        r = client.get("/api/quant/sources")
        assert r.status_code == 200
        assert r.json()["total_records"] == 0

        # novelty timeline
        r = client.get("/api/quant/novelty")
        assert r.status_code == 200
        assert r.json()["total"] == 1

        # novelty for one success
        r = client.get("/api/quant/novelty/cap-1")
        assert r.status_code == 200
        assert r.json()["novelty_score"] == 0.95

        # novelty for one 404
        r = client.get("/api/quant/novelty/cap-2")
        assert r.status_code == 404

        # lead-lag with report
        r = client.get("/api/quant/lead-lag")
        assert r.status_code == 200
        assert r.json()["total_events"] == 5

        # lead-lag without report
        r = client.get("/api/quant/lead-lag")
        assert r.status_code == 200
        assert r.json()["total_events"] == 0

        # regime with report
        r = client.get("/api/quant/regime")
        assert r.status_code == 200
        assert r.json()["bucket_minutes"] == 60

        # regime without report
        r = client.get("/api/quant/regime")
        assert r.status_code == 200
        assert r.json()["bucket_minutes"] == 60
        assert len(r.json()["buckets"]) == 0

        # reaction success
        r = client.get("/api/quant/reaction/cap-1")
        assert r.status_code == 200
        assert r.json()["reaction_score"] == 0.8

        # reaction 404
        r = client.get("/api/quant/reaction/cap-2")
        assert r.status_code == 404

        # sentiment momentum with report
        r = client.get("/api/quant/sentiment-momentum")
        assert r.status_code == 200
        assert r.json()["bucket_minutes"] == 240

        # sentiment momentum without report
        r = client.get("/api/quant/sentiment-momentum")
        assert r.status_code == 200
        assert r.json()["tickers"] == []

        # invalidate
        r = client.post("/api/quant/invalidate")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_engine.invalidate.assert_called_once()


def test_live_read_has_signal_exceptions():
    app = create_app(Settings())
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {"window_records": "invalid"}
    mock_sup = MagicMock()

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "local"


def test_api_quant_live_read_deterministic_fallbacks():
    app = create_app(Settings())
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_sup = MagicMock()

    # 1. No signal
    mock_engine.dashboard_snapshot.return_value = {}
    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "empty_context"

    # 2. DeepSeek disabled
    mock_engine.dashboard_snapshot.return_value = {"n_records_window": 10}
    mock_sup.reviewers.deepseek.return_value = None
    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "deepseek_disabled"

    # 3. Budget exhausted
    mock_client = MagicMock()
    mock_sup.reviewers.deepseek.return_value = mock_client
    mock_sup.reviewers.budget_state.return_value.exhausted = True
    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "budget_exhausted"


def test_api_quant_live_read_deepseek_scenarios():
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

    # 1. DeepSeek Success
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "DeepSeek narrative text"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    mock_client._client.post.return_value = mock_response

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "deepseek"
        assert r.json()["narrative"] == "DeepSeek narrative text"
        assert r.json()["usd_cost"] == 0.05

    # 2. DeepSeek HTTP Error
    mock_response_err = MagicMock()
    mock_response_err.status_code = 500
    mock_client._client.post.return_value = mock_response_err

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "http_500"

    # 3. DeepSeek Connection Error (Exception)
    mock_client._client.post.side_effect = RuntimeError("connection error")
    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/api/quant/live-read")
        assert r.status_code == 200
        assert r.json()["source"] == "local"
        assert r.json()["fallback_reason"] == "connection error"


@pytest.mark.asyncio
async def test_api_quant_live_read_stream_local():
    app = create_app(Settings())
    client = TestClient(app)

    mock_engine = MagicMock()
    mock_engine.dashboard_snapshot.return_value = {}  # empty context -> local
    mock_sup = MagicMock()

    with patch("catchem.api._QUANT_ENGINE", mock_engine), patch("catchem.api._SUPERVISOR", mock_sup):
        response = client.get("/api/quant/live-read-stream")
        assert response.status_code == 200
        # read chunks from stream
        lines = response.text.split("\r\n")
        events = []
        for line in lines:
            if line.startswith("event:"):
                events.append(line)
        assert "event: start" in events
        assert "event: done" in events


@pytest.mark.asyncio
async def test_api_quant_live_read_stream_deepseek_success():
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

    # Mock stream_chat_completion as an async generator
    async def mock_stream(*args, **kwargs):
        yield {"type": "delta", "text": "DeepSeek "}
        yield {"type": "delta", "text": "streamed "}
        yield {"type": "delta", "text": "text"}
        yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        yield {"type": "done"}

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", side_effect=mock_stream),
    ):
        response = client.get("/api/quant/live-read-stream")
        assert response.status_code == 200
        # check chunks
        text = response.text
        assert "DeepSeek " in text
        assert "streamed " in text
        assert "text" in text
        assert "done" in text


@pytest.mark.asyncio
async def test_api_quant_live_read_stream_deepseek_error():
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

    async def mock_stream_error(*args, **kwargs):
        yield {"type": "error", "error": "DeepSeek API error"}

    with (
        patch("catchem.api._QUANT_ENGINE", mock_engine),
        patch("catchem.api._SUPERVISOR", mock_sup),
        patch("catchem.reviewers.deepseek.stream_chat_completion", side_effect=mock_stream_error),
    ):
        response = client.get("/api/quant/live-read-stream")
        assert response.status_code == 200
        assert "done" in response.text


def test_api_index_normal():
    app = create_app(Settings())
    client = TestClient(app)

    response = client.get("/api/_index")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    paths = [p["path"] for p in data["paths"]]
    assert "/api/health/deep" in paths or "/healthz" in paths


def test_favicon_found(tmp_path):
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    dummy_ico = tmp_path / "favicon.ico"
    dummy_ico.write_bytes(b"dummy")

    with patch("catchem.api.get_static_path", return_value=dummy_ico):
        response = client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.content == b"dummy"


def test_ui_demo_paste_empty_record():
    app = create_app(Settings())
    client = TestClient(app)

    mock_result = MagicMock()
    mock_result.capture_id = "demo-1"
    mock_result.jsonl_path = "/path/to/demo-1.jsonl"
    mock_result.processed = 1
    mock_result.skipped = 0
    mock_result.record = None

    with patch("catchem.api._run_demo", return_value=mock_result):
        response = client.post("/ui/demo/paste", json={"title": "Test Title", "text": "Test content"})
        assert response.status_code == 200
        data = response.json()
        assert data["capture_id"] == "demo-1"
        assert data["record"]["is_finance_relevant"] is False
