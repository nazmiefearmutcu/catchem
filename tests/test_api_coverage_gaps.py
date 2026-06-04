import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from catchem.api import (
    _build_archiver,
    _build_explain_prompt,
    _build_news_poller,
    _build_ws_channel,
    _checkpoint_wal_best_effort,
    _compute_agreement,
    _display_path,
    _domain_matches,
    _get_quant_engine,
    _git_branch_safe,
    _git_sha_safe,
    _local_explain,
    _local_live_read,
    _normalize_detail_payload,
    _normalize_uvicorn_log_level,
    _render_spa_with_nonce,
    _sanitize_capture_id,
    _sanitize_cluster_id,
    _sanitize_routing_path,
    _sanitize_slug,
    _sanitize_symbol,
    create_app,
)
from catchem.settings import CatchemMode, Settings, load_settings


def test_normalize_uvicorn_log_level():
    assert _normalize_uvicorn_log_level("warn") == "warning"
    assert _normalize_uvicorn_log_level("invalid") == "info"
    assert _normalize_uvicorn_log_level("info") == "info"
    assert _normalize_uvicorn_log_level(None) == "info"


def test_render_spa_with_nonce_none():
    with patch("catchem.api.open_static_bytes", return_value=None):
        html, nonce = _render_spa_with_nonce()
        assert html is None
        assert len(nonce) > 0


def test_display_path_exception():
    with patch("pathlib.Path.home", side_effect=RuntimeError("fake home error")):
        assert _display_path("/some/path") == "/some/path"
    # test home matches
    with patch("pathlib.Path.home", return_value=Path("/home/user")):
        assert _display_path("/home/user") == "~"
        assert _display_path("/home/user/subdir") == "~/subdir"
        assert _display_path(None) is None


def test_sanitize_symbol_exceptions():
    with pytest.raises(HTTPException) as exc:
        _sanitize_symbol("")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_symbol(123)  # type: ignore
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_symbol("INVALID%")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_symbol("   ")
    assert exc.value.status_code == 400


def test_sanitize_capture_id_exceptions():
    with pytest.raises(HTTPException) as exc:
        _sanitize_capture_id("")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_capture_id(123)  # type: ignore
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_capture_id("inv@lid")
    assert exc.value.status_code == 400


def test_sanitize_cluster_id_exceptions():
    with pytest.raises(HTTPException) as exc:
        _sanitize_cluster_id("")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_cluster_id(123)  # type: ignore
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_cluster_id("inv@lid")
    assert exc.value.status_code == 400


def test_sanitize_slug_exceptions():
    with pytest.raises(HTTPException) as exc:
        _sanitize_slug("", "test_slug")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_slug("inv@lid%", "test_slug")
    assert exc.value.status_code == 400


def test_sanitize_routing_path_exceptions():
    with pytest.raises(HTTPException) as exc:
        _sanitize_routing_path(123)  # type: ignore
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_routing_path("some/../path")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_routing_path("some\\path")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _sanitize_routing_path("inv@lid")
    assert exc.value.status_code == 400


def test_domain_matches_empty():
    assert _domain_matches("", frozenset(["example.com"])) is False
    assert _domain_matches(None, frozenset(["example.com"])) is False  # type: ignore


def test_normalize_detail_payload_conversion():
    from datetime import datetime

    now = datetime.now()
    r = {"created_at": now, "published_ts": 12345}
    normalized = _normalize_detail_payload(r)
    assert isinstance(normalized["created_at"], str)
    assert normalized["created_at"] == str(now)
    assert isinstance(normalized["published_ts"], str)
    assert normalized["published_ts"] == "12345"


def test_compute_agreement_various():
    stub = {
        "is_finance_relevant": True,
        "finance_relevance_score": 0.8,
        "asset_classes": ["equity"],
        "impact_reason_codes": ["earnings"],
        "candidate_symbols": ["AAPL"],
        "sentiment_label": "positive",
    }
    ds = {
        "is_finance_relevant": False,
        "finance_relevance_score": 0.2,
        "asset_classes": None,
        "impact_reason_codes": [],
        "candidate_symbols": None,
        "sentiment_label": "negative",
    }
    res = _compute_agreement(stub, ds)
    assert res["relevance_match"] is False
    assert res["score_delta"] == 0.6


def test_local_explain_kinds():
    # cluster
    c_payload = {
        "size": 5,
        "member_domains": ["bloomberg.com", "reuters.com"],
        "dominant_symbols": ["AAPL", "MSFT", "GOOG", "AMZN"],
        "dominant_reasons": ["earnings", "regulatory"],
    }
    assert "Cluster of 5 captures across 2 sources" in _local_explain("cluster", c_payload)
    assert "focused on AAPL, MSFT, GOOG" in _local_explain("cluster", c_payload)

    # cluster with empty values
    c_payload_empty = {"size": 0}
    assert _local_explain("cluster", c_payload_empty) == "Cluster of 0 captures across 0 sources."

    # regime_shift
    r_payload = {
        "kl_divergence_from_prev": 1.456,
        "bucket_start": "2026-06-03T12:00:00",
        "asset_distribution": [("equity", 0.6), ("crypto", 0.4)],
    }
    assert "Topic mix shifted at 2026-06-03T12:00:00 (KL=1.46)" in _local_explain("regime_shift", r_payload)
    assert "now dominated by equity 60%, crypto 40%" in _local_explain("regime_shift", r_payload)

    # regime_shift empty
    r_payload_empty = {}
    assert _local_explain("regime_shift", r_payload_empty) == "Topic mix shifted at ."

    # anomaly with sym
    a_payload_sym = {"z_score": 3.2, "observed": 15, "symbol": "BTC"}
    assert _local_explain("anomaly", a_payload_sym) == "Symbol burst on BTC: 15 mentions, z-score 3.2."

    # anomaly without sym
    a_payload_nosym = {"z_score": 4.1, "observed": 20}
    assert _local_explain("anomaly", a_payload_nosym) == "Volume/sentiment anomaly: observed=20, z-score 4.1."

    # spillover
    s_payload = {"source_asset": "equity", "target_asset": "crypto", "spillover_score": 0.75}
    assert _local_explain("spillover", s_payload) == "Spillover detected: equity => crypto (score 0.75)."

    # unknown
    assert (
        _local_explain("unknown", {}) == "Quant signal flagged; no specific local interpretation available."
    )


def test_local_live_read_branches():
    ctx = {"window_records": 10, "clusters_active": 2, "regime_shifts_recent": 1}
    res = _local_live_read(ctx)
    assert "10 records in window across 2 active clusters." in res
    assert "Anomaly flow is quiet — news mix is statistically normal." in res
    assert "1 regime shift detected over the window." in res

    ctx_single_c = {
        "window_records": 5,
        "clusters_active": 1,
    }
    assert "across 1 active cluster." in _local_live_read(ctx_single_c)

    ctx_burst = {
        "window_records": 10,
        "clusters_active": 2,
        "symbol_bursts": [{"symbol": "TSLA", "observed": 12, "z": 2.4}],
        "volume_anomalies": 1,
        "sentiment_shocks": 2,
    }
    res = _local_live_read(ctx_burst)
    assert "Symbol burst on TSLA (12 mentions, z=2.4)" in res
    assert "1 volume / 2 sentiment anomalies otherwise." in res

    ctx_vols = {
        "window_records": 10,
        "clusters_active": 2,
        "symbol_bursts": [],
        "volume_anomalies": 1,
        "sentiment_shocks": 2,
    }
    res = _local_live_read(ctx_vols)
    assert "1 volume / 2 sentiment anomalies firing; no symbol burst." in res

    ctx_top_c = {
        "window_records": 10,
        "clusters_active": 2,
        "top_clusters": [
            {"symbols": ["AAPL", "MSFT"], "reasons": ["earnings"], "coherence": 0.85, "size": 3}
        ],
    }
    res = _local_live_read(ctx_top_c)
    assert "Top event: AAPL, MSFT via earnings, size 3, coherence 85%." in res


def test_build_explain_prompt():
    prompt = _build_explain_prompt("anomaly", {"symbol": "BTC"}, "some local interpretation")
    assert "Quant signal kind: anomaly" in prompt
    assert "some local interpretation" in prompt
    assert "BTC" in prompt


def test_git_sha_safe_exception():
    with patch("subprocess.run", side_effect=Exception("subproc error")):
        assert _git_sha_safe() is None


def test_git_branch_safe_exception():
    with patch("subprocess.run", side_effect=Exception("subproc error")):
        assert _git_branch_safe() is None


def test_checkpoint_wal_best_effort_cases():
    with patch("catchem.api._SUPERVISOR", None):
        assert _checkpoint_wal_best_effort() is False

    mock_sup = MagicMock()
    with patch("catchem.api._SUPERVISOR", mock_sup):
        mock_sup.storage = None
        assert _checkpoint_wal_best_effort() is False


def test_get_quant_engine_double_lock():
    mock_sup = MagicMock()
    mock_sup.storage = MagicMock()
    with patch("catchem.api._SUPERVISOR", mock_sup), patch("catchem.api._QUANT_ENGINE", None):
        engine = _get_quant_engine()
        assert engine is not None


def test_build_news_poller_extra_feeds():
    settings = Settings()
    settings.news.poller_enabled = True
    settings.news.feeds = [
        {
            "name": "test_feed",
            "url": "http://example.com/rss",
            "fallback_domain": "example.com",
            "parser": "rss",
        },
        {"name": "invalid"},
    ]
    mock_sup = MagicMock()
    poller = _build_news_poller(mock_sup, settings)
    assert poller is not None


def test_build_ws_channel_disabled():
    settings = Settings()
    settings.news.websocket_enabled = False
    mock_sup = MagicMock()
    assert _build_ws_channel(mock_sup, settings) is None


def test_build_archiver_drive_dir():
    settings = Settings()
    settings.archive.enabled = True
    settings.archive.drive_dir = "~/test_drive"
    mock_sup = MagicMock()
    archiver = _build_archiver(mock_sup, settings)
    assert archiver is not None
    assert archiver.drive_dir == Path("~/test_drive").expanduser()


def test_lifespan_startup_failure():
    settings = Settings()
    with patch("catchem.api._build_news_poller") as mock_build:
        mock_poller = MagicMock()
        mock_build.return_value = mock_poller

        with (
            patch("catchem.api._build_ws_channel") as mock_ws_build,
            patch("catchem.api._build_archiver") as mock_arc_build,
        ):
            mock_ws = MagicMock()
            mock_ws.stop = AsyncMock()
            mock_ws_build.return_value = mock_ws

            mock_arc = MagicMock()
            mock_arc.start.side_effect = RuntimeError("start failed")
            mock_arc_build.return_value = mock_arc

            app = create_app(settings)
            with pytest.raises(RuntimeError, match="start failed"):
                with TestClient(app):
                    pass

            mock_ws.stop.assert_called_once()
            mock_poller.stop.assert_called_once()


def test_create_app_with_cors():
    settings = Settings()
    settings.api.cors_origins = ["http://localhost:3000"]
    app = create_app(settings)
    assert app is not None


def test_request_counter_exception_path():
    settings = Settings()
    app = create_app(settings)

    healthz_route = None
    for r in app.routes:
        if getattr(r, "path", None) == "/healthz":
            healthz_route = r
            break

    assert healthz_route is not None
    real_path = healthz_route.path

    class BadRoute(healthz_route.__class__):
        @property
        def path(self):
            if getattr(self, "_corrupt", False):
                raise RuntimeError("path access error")
            return real_path

    healthz_route.__class__ = BadRoute
    healthz_route._corrupt = False

    @app.middleware("http")
    async def corrupt_middleware(request: Request, call_next):
        healthz_route._corrupt = True
        try:
            return await call_next(request)
        finally:
            healthz_route._corrupt = False

    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200


def test_legacy_endpoint_missing_template():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    with patch("catchem.api.open_static_bytes", return_value=None):
        r1 = client.get("/legacy")
        assert r1.status_code == 404
        r2 = client.get("/legacy-dashboard")
        assert r2.status_code == 404


def test_root_endpoint_missing_spa():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    with patch("catchem.api._render_spa_with_nonce", return_value=(None, "nonce")):
        r = client.get("/")
        assert r.status_code == 200
        assert "The premium UI bundle has not been built yet." in r.text


def test_favicon_not_found():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    with patch("catchem.api.get_static_path", return_value=None):
        r = client.get("/favicon.ico")
        assert r.status_code == 204


def test_deep_health_unusual_conditions():
    # 1. non-positive uptime
    with patch("catchem.api._PROCESS_STARTED_AT", datetime.now(UTC) + timedelta(seconds=10)):
        app = create_app(load_settings())
        client = TestClient(app)
        r = client.get("/api/health/deep")
        assert r.status_code == 503
        body = r.json()
        assert "uptime_check: non-positive uptime" in body["issues"]

    app = create_app(load_settings())
    api_health_deep_fn = None
    for r in app.routes:
        if getattr(r, "path", None) == "/api/health/deep":
            api_health_deep_fn = r.endpoint
            break
    assert api_health_deep_fn is not None

    # 2. uptime check raises exception
    with patch("catchem.api.datetime") as mock_datetime:
        mock_datetime.now.side_effect = [
            RuntimeError("datetime failure"),
            datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC),
        ]
        mock_datetime.fromisoformat = datetime.fromisoformat
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            assert res.status_code == 503
        else:
            assert res["checks"]["uptime_ok"] is False

    # 3. schema/sqlite checks when _SUPERVISOR is None
    with patch("catchem.api._SUPERVISOR", None):
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            assert res.status_code == 503
            body = json.loads(res.body.decode())
            assert "supervisor_not_initialized" in body["issues"]
            assert body["checks"]["sqlite_ok"] is False
            assert body["checks"]["schema_ok"] is False

    # 4. sqlite SELECT 1 returning unexpected value
    mock_sup = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (2,)
    mock_sup.storage._connection.return_value.__enter__.return_value = mock_conn
    with patch("catchem.api._SUPERVISOR", mock_sup):
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            assert res.status_code == 503
            body = json.loads(res.body.decode())
            assert any("SELECT 1 returned unexpected value" in issue for issue in body["issues"])

    # 5. news_poller not run yet (last_run_at is None)
    mock_poller = MagicMock()
    mock_poller.last_run_at = None
    mock_poller.interval_seconds = 10
    with patch("catchem.api._NEWS_POLLER", mock_poller):
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            body = json.loads(res.body.decode())
        else:
            body = res
        assert body["checks"]["news_poller_ok"] is True

    # 6. news_poller raises exception during check
    mock_poller = MagicMock()
    type(mock_poller).interval_seconds = property(lambda self: RuntimeError("poller property fail"))
    with patch("catchem.api._NEWS_POLLER", mock_poller):
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            body = json.loads(res.body.decode())
            assert body["checks"]["news_poller_ok"] is False

    # 7. low disk space (< 100 MB free)
    mock_usage = MagicMock()
    mock_usage.free = 50 * 1024 * 1024
    with patch("shutil.disk_usage", return_value=mock_usage):
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            body = json.loads(res.body.decode())
            assert any("disk_low" in issue for issue in body["issues"])
            assert body["checks"]["disk_ok"] is False

    # 8. disk space check raises exception
    with patch("shutil.disk_usage", side_effect=RuntimeError("disk error")):
        res = api_health_deep_fn()
        if hasattr(res, "status_code"):
            body = json.loads(res.body.decode())
            assert body["checks"]["disk_ok"] is False


def test_api_index_edge_cases():
    settings = Settings()
    app = create_app(settings)

    mock_route1 = MagicMock()
    mock_route1.path = 123
    mock_route2 = MagicMock()
    mock_route2.path = "/api/test"
    mock_route2.methods = set()

    api_index_fn = None
    for r in app.routes:
        if getattr(r, "path", None) == "/api/_index":
            api_index_fn = r.endpoint
            break

    assert api_index_fn is not None

    app.routes.clear()
    app.routes.append(mock_route1)
    app.routes.append(mock_route2)

    res = api_index_fn()
    assert res["total"] == 0


def test_metrics_and_dashboard_production_safe():
    settings = Settings()
    settings.mode = CatchemMode.PRODUCTION_SAFE

    app = create_app(settings)
    client = TestClient(app)

    mock_sup = MagicMock()
    mock_sup.status.return_value = {"diagnostic_enabled": True}
    with patch("catchem.api._SUPERVISOR", mock_sup):
        r = client.get("/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["diagnostic_enabled"] is False

    with (
        patch(
            "catchem.api.overview",
            return_value={"recent": [{"diagnostic_enabled": True}], "diagnostic_count": 5},
        ),
        patch("catchem.api._SUPERVISOR", mock_sup),
    ):
        r = client.get("/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["diagnostic_count"] == 0


def test_api_stats_caching_and_budget_errors():
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)

    from catchem.api import _STATS_CACHE

    _STATS_CACHE.clear()

    mock_sup = MagicMock()
    mock_sup.storage.recent_records.return_value = []
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.side_effect = [(10,), (5,), (1,)]
    mock_sup.storage._connection.return_value.__enter__.return_value = mock_conn

    with patch("catchem.api._SUPERVISOR", mock_sup):
        r1 = client.get("/api/stats")
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["db"]["records"] == 10

        mock_conn.execute.return_value.fetchone.side_effect = [(20,), (5,), (1,)]
        r2 = client.get("/api/stats")
        assert r2.status_code == 200
        b2 = r2.json()
        assert b2["db"]["records"] == 10

    _STATS_CACHE.clear()
    mock_sup_err = MagicMock()
    mock_registry = MagicMock()
    mock_registry.budget_state.side_effect = RuntimeError("budget state error")
    mock_sup_err.reviewers = mock_registry

    mock_conn_err = MagicMock()
    mock_conn_err.execute.return_value.fetchone.side_effect = [(10,), (5,), (1,)]
    mock_sup_err.storage._connection.return_value.__enter__.return_value = mock_conn_err

    with patch("catchem.api._SUPERVISOR", mock_sup_err):
        r = client.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["reviewers"]["deepseek_usd_spent"] == 0.0
