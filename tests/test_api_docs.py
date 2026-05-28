"""OpenAPI docs and /api/_index contract.

FastAPI's default doc paths (/docs, /redoc, /openapi.json) collide with
the SPA root + history-mode catch-all. Catchem re-mounts them under /api/*
where the SPA never claims and adds /api/_index for programmatic consumers
(help drawer, debug overlays, agents discovering the surface).

These tests pin:
  * /api/docs and /api/redoc serve HTML (Swagger UI / Redoc shell)
  * /api/openapi.json is valid JSON with an `openapi` key
  * /api/_index returns {paths, total, schema_version}
  * The schema does not leak the env-var name behind any ApiKey-gated route
  * Legacy /docs et al. correctly 404 instead of returning the SPA shell
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


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


# ── /api/docs (Swagger UI) ──────────────────────────────────────────────────

def test_api_docs_serves_swagger_ui(client: TestClient) -> None:
    r = client.get("/api/docs")
    assert r.status_code == 200, r.text
    ctype = r.headers.get("content-type", "")
    assert "text/html" in ctype.lower()
    # Swagger UI shell references the openapi.json relative URL we configured.
    body = r.text
    assert "/api/openapi.json" in body
    # Should look like a Swagger page, not the SPA fallback
    assert "swagger" in body.lower() or "SwaggerUIBundle" in body


# ── /api/redoc ──────────────────────────────────────────────────────────────

def test_api_redoc_serves_html(client: TestClient) -> None:
    r = client.get("/api/redoc")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers.get("content-type", "").lower()
    assert "/api/openapi.json" in r.text


# ── /api/openapi.json ───────────────────────────────────────────────────────

def test_api_openapi_json_valid(client: TestClient) -> None:
    r = client.get("/api/openapi.json")
    assert r.status_code == 200, r.text
    schema = r.json()
    assert "openapi" in schema
    assert isinstance(schema["openapi"], str)
    assert schema["openapi"].startswith("3.")  # OpenAPI 3.x
    assert "paths" in schema and isinstance(schema["paths"], dict)
    # The schema should expose at least the docs-meta paths plus real routes.
    # `/api/_index` and `/healthz` are good canaries (always registered).
    assert "/api/_index" in schema["paths"]
    assert "/healthz" in schema["paths"]


def test_api_openapi_does_not_leak_env_var_names(client: TestClient) -> None:
    """ApiKey-protected routes must not surface raw env-var names in the schema."""
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    blob = r.text
    # The two sensitive env-var names actually present in Settings.
    # If a future route adds Security(APIKeyHeader(name="CATCHEM_..."))
    # without redacting, this test will flag it before it ships.
    forbidden_substrings = (
        "CATCHEM_DEEPSEEK_API_KEY",
        "CATCHEM_GUARDS__SECRET",
    )
    for needle in forbidden_substrings:
        assert needle not in blob, f"OpenAPI schema leaks env-var name: {needle}"


# ── /api/_index ─────────────────────────────────────────────────────────────

def test_api_index_shape(client: TestClient) -> None:
    r = client.get("/api/_index")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"paths", "total", "schema_version"}
    assert body["schema_version"] == 1
    assert isinstance(body["paths"], list)
    assert body["total"] == len(body["paths"])
    assert body["total"] > 0, "no routes discovered — index is empty"


def test_api_index_entries_have_required_fields(client: TestClient) -> None:
    r = client.get("/api/_index")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for entry in paths:
        assert "path" in entry
        assert "method" in entry
        assert "summary" in entry
        assert entry["path"].startswith(("/api", "/ui", "/healthz"))
        # methods are uppercased + HEAD/OPTIONS stripped
        assert entry["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_api_index_is_sorted(client: TestClient) -> None:
    r = client.get("/api/_index")
    paths = r.json()["paths"]
    sorted_paths = sorted(paths, key=lambda p: (p["path"], p["method"]))
    assert paths == sorted_paths


def test_api_index_contains_known_routes(client: TestClient) -> None:
    """Sanity: /healthz and /api/_index itself must appear in the listing."""
    r = client.get("/api/_index")
    paths = r.json()["paths"]
    flat = {(p["path"], p["method"]) for p in paths}
    assert ("/healthz", "GET") in flat
    assert ("/api/_index", "GET") in flat


# ── SPA fallback behavior on legacy doc paths ───────────────────────────────

def test_legacy_docs_path_does_not_serve_spa(client: TestClient) -> None:
    """Old /docs path should 404 (SPA reserved-prefix list still excludes it)."""
    r = client.get("/docs")
    # 404 is the contract — never the SPA shell
    assert r.status_code == 404
    # Body must not be HTML index.html
    assert "<!doctype html>" not in r.text.lower() or "not_found" in r.text.lower()


def test_legacy_openapi_path_404s(client: TestClient) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 404


def test_unknown_api_path_404s_not_spa(client: TestClient) -> None:
    """Typo'd /api/foo should 404 rather than return the SPA bundle."""
    r = client.get("/api/this-does-not-exist")
    assert r.status_code == 404
    # Confirms reserved-prefix gate fired — body is a JSON error, not HTML
    try:
        payload = r.json()
        # FastAPI default error shape or our HTTPException detail
        assert "detail" in payload or "error" in payload or "not_found" in r.text
    except json.JSONDecodeError:
        # Some FastAPI 404s ship with content-type text/plain — still fine
        # as long as it isn't the SPA bundle.
        assert "<!doctype html" not in r.text.lower()
