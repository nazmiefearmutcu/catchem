"""FastAPI surface for the local fusion stack.

Endpoints are intentionally thin pass-throughs to the Supervisor. Auth is
out-of-scope (local-first). The API binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .dashboard_data import overview
from .logging import get_logger
from .schemas import AwarenessCaptureView
from .settings import Settings, load_settings
from .supervisor import Supervisor


_STATIC_DIR = Path(__file__).resolve().parent / "static"


logger = get_logger("fusion.api")

_SUPERVISOR: Supervisor | None = None
_SETTINGS: Settings | None = None


def _get_supervisor() -> Supervisor:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        raise HTTPException(status_code=503, detail="supervisor_not_initialized")
    return _SUPERVISOR


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    global _SUPERVISOR, _SETTINGS
    _SETTINGS = load_settings()
    _SUPERVISOR = Supervisor(_SETTINGS)
    try:
        yield
    finally:
        if _SUPERVISOR is not None:
            _SUPERVISOR.close()
        _SUPERVISOR = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory. Tests can pass a Settings instance; CLI uses lifespan loading."""
    app = FastAPI(title="fusion_stack", version="0.1.0", lifespan=lifespan)

    cors = (settings or Settings()).api.cors_origins
    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        path = _STATIC_DIR / "dashboard.html"
        if not path.exists():
            return HTMLResponse("<h1>dashboard template missing</h1>", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/config")
    def config() -> dict[str, Any]:
        s = _SETTINGS or load_settings()
        return {
            "mode": s.mode.value,
            "use_ml_stubs": s.models_.use_ml_stubs,
            "newsimpact_diagnostic_enabled": s.guards.newsimpact_diagnostic_enabled,
            "diagnostic_allowed": s.diagnostic_allowed(),
            "model_versions": dict(_get_supervisor().service.model_versions),
        }

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        sup = _get_supervisor()
        return sup.status()

    @app.get("/recent")
    def recent(limit: int = Query(50, ge=1, le=500), relevant_only: bool = True) -> dict[str, Any]:
        sup = _get_supervisor()
        return {"items": sup.storage.recent_records(limit=limit, relevant_only=relevant_only)}

    @app.get("/dashboard")
    def dashboard(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        sup = _get_supervisor()
        return overview(sup.storage, limit=limit)

    @app.get("/record/{capture_id}")
    def record(capture_id: str) -> dict[str, Any]:
        sup = _get_supervisor()
        rec = sup.storage.get_record(capture_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="capture_not_found")
        return rec

    @app.get("/records/by-symbol/{symbol}")
    def by_symbol(symbol: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        sup = _get_supervisor()
        return {"items": sup.storage.by_label("symbol", symbol, limit=limit)}

    @app.get("/records/by-asset-class/{asset_class}")
    def by_asset_class(asset_class: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        sup = _get_supervisor()
        return {"items": sup.storage.by_label("asset_class", asset_class, limit=limit)}

    @app.get("/records/by-reason/{reason_code}")
    def by_reason(reason_code: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        sup = _get_supervisor()
        return {"items": sup.storage.by_label("reason_code", reason_code, limit=limit)}

    @app.post("/replay")
    def replay(max_records: int = Body(50, embed=True)) -> dict[str, Any]:
        sup = _get_supervisor()
        return sup.run_replay(max_records=max_records)

    @app.post("/process-one")
    def process_one(capture: dict = Body(...)) -> dict[str, Any]:
        sup = _get_supervisor()
        cap = AwarenessCaptureView.model_validate(capture)
        rec = sup.process_capture(cap)
        return rec.model_dump(mode="json")

    return app


def run() -> None:
    """Entry point for the ``fusion-stack-api`` console script."""
    s = load_settings()
    app = create_app(s)
    host = os.getenv("FUSION_API_HOST", s.api.host)
    port = int(os.getenv("FUSION_API_PORT", s.api.port))
    uvicorn.run(app, host=host, port=port, log_level=s.logging_.level.lower())


# Module-level app for `uvicorn fusion_stack.api:app`
app = create_app()
