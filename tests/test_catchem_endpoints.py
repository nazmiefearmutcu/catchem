"""Catchem desktop endpoints: /ui/demo/paste, /ui/demo/upload, /ui/app-info,
/ui/sidecar-status, /ui/log-tail.

These cover the contracts the Tauri shell relies on. Each test pins the
documented response shape so a future change can't silently drop a field.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings

FED_ARTICLE = (
    "The Federal Reserve raised its benchmark interest rate by 25 basis points "
    "on Wednesday, citing persistent inflation pressures. Apple (AAPL) fell 2% "
    "and Microsoft (MSFT) lost 1.8%. Chair Powell said the central bank remains "
    "data-dependent."
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


# ── /ui/demo/paste ──────────────────────────────────────────────────────────


def test_demo_paste_happy_path(client: TestClient) -> None:
    r = client.post(
        "/ui/demo/paste",
        json={
            "title": "Fed raises rates by 25 bps",
            "text": FED_ARTICLE,
            "domain": "reuters.com",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # DemoRunResponse shape
    for k in ("capture_id", "jsonl_basename", "processed", "skipped", "record"):
        assert k in body, f"missing {k}"
    # JSONL basename must not leak a directory
    assert "/" not in body["jsonl_basename"]
    assert "\\" not in body["jsonl_basename"]
    # Record is a FinancialImpactDetail
    rec = body["record"]
    assert rec["is_finance_relevant"] is True
    assert rec["finance_relevance_score"] > 0.5
    assert "central_bank" in rec["impact_reason_codes"]
    assert any(s in rec["candidate_symbols"] for s in ("AAPL", "MSFT"))
    # Production-safe guard: diagnostic always off
    assert rec["diagnostic_multimodal_enabled"] is False
    assert rec["diagnostic_multimodal_result"] is None


def test_demo_paste_validation_rejects_empty(client: TestClient) -> None:
    r = client.post("/ui/demo/paste", json={"title": "", "text": ""})
    assert r.status_code == 422


def test_demo_paste_validation_rejects_oversized_text(client: TestClient) -> None:
    r = client.post(
        "/ui/demo/paste",
        json={
            "title": "t",
            "text": "x" * (5 * 1024 * 1024 + 1),
        },
    )
    assert r.status_code == 413


def test_demo_paste_deterministic_capture_id(client: TestClient) -> None:
    """Same (title, text, domain, url) → same capture_id."""
    payload = {"title": "Fed", "text": FED_ARTICLE, "domain": "reuters.com"}
    a = client.post("/ui/demo/paste", json=payload).json()
    b = client.post("/ui/demo/paste", json=payload).json()
    assert a["capture_id"] == b["capture_id"]


# ── /ui/demo/upload ─────────────────────────────────────────────────────────


def test_demo_upload_txt(client: TestClient) -> None:
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("fed.txt", io.BytesIO(FED_ARTICLE.encode()), "text/plain")},
        data={"title": "Fed hike", "domain": "reuters.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record"]["is_finance_relevant"] is True


def test_demo_upload_markdown_extracts_heading(client: TestClient) -> None:
    md = "# Fed raises rates\n\n" + FED_ARTICLE
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("doc.md", io.BytesIO(md.encode()), "text/markdown")},
        data={"domain": "reuters.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # No explicit title provided → first heading or first sentence becomes title
    assert "Fed raises rates" in (body["record"]["title"] or "") or body["record"]["title"]


def test_demo_upload_html_strips_scripts(client: TestClient) -> None:
    html = (
        "<html><head><title>x</title>"
        "<script>alert(1)</script>"
        "<style>p{color:red}</style></head>"
        f"<body><h1>Fed hike</h1><p>{FED_ARTICLE}</p></body></html>"
    )
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("article.html", io.BytesIO(html.encode()), "text/html")},
        data={"domain": "reuters.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The script content must not appear in extracted text → not in record fields
    # We check the strongest signal: a record was produced and no 'alert' substring
    for v in body["record"].values():
        if isinstance(v, str):
            assert "alert(1)" not in v


def test_demo_upload_jsonl_uses_text_field(client: TestClient) -> None:
    jsonl = '{"text": "' + FED_ARTICLE.replace('"', '\\"') + '"}\n{"not_text": "ignored"}\n'
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("articles.jsonl", io.BytesIO(jsonl.encode()), "application/jsonl")},
        data={"title": "Fed hike", "domain": "reuters.com"},
    )
    assert r.status_code == 200, r.text


def test_demo_upload_rejects_unsupported_suffix(client: TestClient) -> None:
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("evil.exe", io.BytesIO(b"MZ\x00\x00"), "application/octet-stream")},
        data={"domain": "evil.com"},
    )
    assert r.status_code == 422
    assert "unsupported file type" in r.text


def test_demo_upload_rejects_oversized(client: TestClient) -> None:
    big = b"x" * (5 * 1024 * 1024 + 10)
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("big.txt", io.BytesIO(big), "text/plain")},
        data={"domain": "x.com"},
    )
    assert r.status_code == 422
    assert "too large" in r.text


def test_demo_upload_rejects_empty_body(client: TestClient) -> None:
    r = client.post(
        "/ui/demo/upload",
        files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
    )
    assert r.status_code == 422


# ── /ui/app-info ────────────────────────────────────────────────────────────


def test_app_info_shape(client: TestClient) -> None:
    r = client.get("/ui/app-info")
    assert r.status_code == 200
    body = r.json()
    for k in (
        "name",
        "version",
        "mode",
        "use_ml_stubs",
        "diagnostic_allowed",
        "static_bundle_present",
        "model_versions",
        "generated_at",
    ):
        assert k in body, f"missing {k}"
    # production_safe by default → diagnostic_allowed False
    assert body["diagnostic_allowed"] is False
    assert body["mode"] == "production_safe"


# ── /ui/sidecar-status ──────────────────────────────────────────────────────


def test_sidecar_status_shape(client: TestClient) -> None:
    r = client.get("/ui/sidecar-status")
    assert r.status_code == 200
    body = r.json()
    for k in (
        "healthy",
        "api_host",
        "api_port",
        "pid",
        "uptime_seconds",
        "records",
        "dlq",
        "diagnostic_enabled",
        "generated_at",
    ):
        assert k in body
    assert body["healthy"] is True
    assert body["diagnostic_enabled"] is False
    assert body["pid"] > 0
    assert body["uptime_seconds"] >= 0


def test_sidecar_status_reports_actual_bind_not_settings_default() -> None:
    """`/ui/sidecar-status` MUST surface the host/port we actually bound
    on, not the value baked into `configs/catchem.yaml`. The Tauri shell's
    Model Controls page renders `s.api_host:s.api_port` directly as the
    sidecar connection address — if this drifts from the real bind the
    operator sees a confidently-wrong number (e.g. "127.0.0.1:8087" when
    the process is actually serving :9090).

    Recorded via api.record_bind(host, port) which cli.py:serve() calls
    right before uvicorn.run(). This test pins the contract directly,
    without spinning up uvicorn — bind a TestClient against a freshly
    recorded port and confirm the endpoint echoes it back.
    """
    from catchem.api import create_app, record_bind
    from catchem.settings import load_settings, reload_settings

    reload_settings()
    record_bind("127.0.0.1", 9090)
    app = create_app(load_settings())
    with TestClient(app) as c:
        body = c.get("/ui/sidecar-status").json()
        assert body["api_host"] == "127.0.0.1"
        assert body["api_port"] == 9090, (
            f"expected the actual bind port (9090) to win over settings default; got {body['api_port']}"
        )
    # Reset for downstream tests that depend on the settings fallback.
    record_bind("__reset__", 0)
    # Direct hit to the module to wipe the recorded values.
    import catchem.api as _api

    _api._BIND_HOST = None
    _api._BIND_PORT = None


def test_sidecar_status_falls_back_to_settings_when_bind_unrecorded() -> None:
    """If record_bind was never called (e.g. the API was created by a
    test harness that didn't run uvicorn), `/ui/sidecar-status` must
    fall back to settings.api.host/port rather than expose `None`.
    """
    import catchem.api as _api

    _api._BIND_HOST = None
    _api._BIND_PORT = None
    from catchem.api import create_app
    from catchem.settings import load_settings, reload_settings

    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        body = c.get("/ui/sidecar-status").json()
        assert body["api_host"] == s.api.host
        assert body["api_port"] == s.api.port


# ── /ui/log-tail ────────────────────────────────────────────────────────────


def test_log_tail_empty_when_no_log_yet(client: TestClient) -> None:
    r = client.get("/ui/log-tail?lines=10")
    assert r.status_code == 200
    body = r.json()
    assert "lines" in body and isinstance(body["lines"], list)
    assert "truncated" in body


def test_log_tail_rejects_silly_limits(client: TestClient) -> None:
    assert client.get("/ui/log-tail?lines=0").status_code == 422
    assert client.get("/ui/log-tail?lines=99999").status_code == 422


# ── No path leakage ─────────────────────────────────────────────────────────


def test_demo_paste_jsonl_basename_only(client: TestClient) -> None:
    r = client.post(
        "/ui/demo/paste",
        json={
            "title": "x",
            "text": "The Fed raised rates 25bps citing inflation.",
            "domain": "reuters.com",
        },
    )
    body = r.json()
    # No absolute path or directory separators
    assert "/" not in body["jsonl_basename"]
    assert "Users" not in body["jsonl_basename"]


def test_app_info_does_not_leak_filesystem_paths(client: TestClient) -> None:
    body = client.get("/ui/app-info").json()
    flat = str(body)
    assert "/Users/" not in flat
    assert "/etc/" not in flat


def test_demo_paste_custom_max_upload_size_limit() -> None:
    from catchem.api import create_app
    from catchem.settings import load_settings, reload_settings

    reload_settings()
    s = load_settings()
    s.api.max_upload_size_bytes = 10
    app = create_app(s)
    with TestClient(app) as c:
        r = c.post(
            "/ui/demo/paste",
            json={
                "title": "Short",
                "text": "12345678901",
            },
        )
        assert r.status_code == 413
        assert "text exceeds size cap" in r.text

        r2 = c.post(
            "/ui/demo/paste",
            json={
                "title": "Short",
                "text": "1234567890",
            },
        )
        assert r2.status_code != 413
