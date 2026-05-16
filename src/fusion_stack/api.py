"""FastAPI surface for the local fusion stack.

Endpoints are intentionally thin pass-throughs to the Supervisor. Auth is
out-of-scope (local-first). The API binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import os
import asyncio
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .dashboard_data import overview
from .logging import get_logger
from .newsimpact_guarded_adapter import snapshot_guard_state, NewsImpactGuardError
from .schemas import AwarenessCaptureView
from .settings import Settings, load_settings
from .supervisor import Supervisor


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_APP_BUNDLE_DIR = _STATIC_DIR / "app"


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

    # ── Legacy vanilla dashboard (kept until the premium app fully replaces it)
    @app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/legacy-dashboard", response_class=HTMLResponse, include_in_schema=False)
    def legacy() -> HTMLResponse:
        path = _STATIC_DIR / "dashboard.html"
        if not path.exists():
            return HTMLResponse("<h1>dashboard template missing</h1>", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    # ── Premium SPA bundle served at /
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root() -> HTMLResponse:
        index = _APP_BUNDLE_DIR / "index.html"
        if index.exists():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        # Friendly fallback when the bundle hasn't been built yet
        msg = (
            "<!doctype html><meta charset=utf-8><title>fusion_stack</title>"
            "<style>body{font-family:ui-monospace,monospace;background:#0e1014;color:#e7ebf0;"
            "padding:48px;max-width:640px;line-height:1.6}h1{color:#5fb3ff;font-size:18px}"
            "code{background:#161922;padding:2px 6px;border-radius:4px}a{color:#5fb3ff}</style>"
            "<h1>fusion_stack</h1>"
            "<p>The premium UI bundle has not been built yet.</p>"
            "<p>Run <code>bash scripts/fusion_bootstrap_and_run.sh</code> "
            "or <code>(cd frontend && npm install && npm run build)</code>.</p>"
            "<p>Legacy dashboard meanwhile: <a href=\"/legacy\">/legacy</a></p>"
        )
        return HTMLResponse(msg, status_code=200)

    # Mount the built bundle's static assets if they exist
    if _APP_BUNDLE_DIR.exists() and (_APP_BUNDLE_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(_APP_BUNDLE_DIR / "assets")), name="assets")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        ico = _APP_BUNDLE_DIR / "favicon.ico"
        if ico.exists():
            return FileResponse(ico)
        return Response(status_code=204)

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

    # ────────────────────────────────────────────────────────────────────────
    # /ui/* — aggregation endpoints for the premium frontend.
    # Lean payloads, typed JSON, no overfetching.
    # These do NOT replace /recent etc. — they coexist for UI ergonomics.
    # ────────────────────────────────────────────────────────────────────────

    @app.get("/ui/summary")
    def ui_summary() -> dict[str, Any]:
        """Compact landing payload. Single round-trip for the Overview page."""
        sup = _get_supervisor()
        dash = overview(sup.storage, limit=50)
        s = _SETTINGS or load_settings()
        guards = _guard_snapshot(s)
        return {
            "mode": s.mode.value,
            "is_production_safe": s.is_production_safe(),
            "diagnostic_allowed": s.diagnostic_allowed(),
            "use_ml_stubs": s.models_.use_ml_stubs,
            "totals": dash["totals"],
            "diagnostic_count": dash["diagnostic_count"],
            "asset_class_distribution": dash["asset_class_distribution"],
            "reason_code_distribution": dash["reason_code_distribution"],
            "sentiment_distribution": dash["sentiment_distribution"],
            "recent_top": dash["recent"][:6],
            "dlq": sup.storage.dlq_count(),
            "model_versions": dict(sup.service.model_versions),
            "guards": guards,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/ui/facets")
    def ui_facets(limit: int = Query(500, ge=10, le=2000)) -> dict[str, Any]:
        """Facets over recent N records — for filter chip populations."""
        sup = _get_supervisor()
        rows = sup.storage.recent_records(limit=limit, relevant_only=False)
        ac, rc, sym, dom, sent = Counter(), Counter(), Counter(), Counter(), Counter()
        relevant_n = 0
        for r in rows:
            if r.get("is_finance_relevant"):
                relevant_n += 1
            for x in r.get("asset_classes", []):
                ac[x] += 1
            for x in r.get("impact_reason_codes", []):
                rc[x] += 1
            for x in r.get("candidate_symbols", []):
                sym[x] += 1
            if r.get("domain"):
                dom[r["domain"]] += 1
            if r.get("sentiment_label"):
                sent[r["sentiment_label"]] += 1
        return {
            "window_total": len(rows),
            "window_relevant": relevant_n,
            "asset_classes": ac.most_common(),
            "reason_codes": rc.most_common(),
            "symbols": sym.most_common(50),
            "domains": dom.most_common(50),
            "sentiments": sent.most_common(),
        }

    @app.get("/ui/timeline")
    def ui_timeline(bucket_minutes: int = Query(60, ge=5, le=1440),
                    limit: int = Query(500, ge=10, le=5000)) -> dict[str, Any]:
        """Timestamp-bucketed counts for trend charts."""
        sup = _get_supervisor()
        rows = sup.storage.recent_records(limit=limit, relevant_only=False)
        buckets: dict[str, dict[str, int]] = {}
        for r in rows:
            ts = r.get("published_ts") or r.get("created_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            minute = (dt.minute // bucket_minutes) * bucket_minutes
            key = dt.replace(minute=minute, second=0, microsecond=0).isoformat()
            b = buckets.setdefault(key, {"total": 0, "relevant": 0})
            b["total"] += 1
            if r.get("is_finance_relevant"):
                b["relevant"] += 1
        series = [{"ts": k, **v} for k, v in sorted(buckets.items())]
        return {"bucket_minutes": bucket_minutes, "series": series}

    @app.get("/ui/top-symbols")
    def ui_top_symbols(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        sup = _get_supervisor()
        rows = sup.storage.recent_records(limit=500, relevant_only=True)
        c = Counter()
        for r in rows:
            for s in r.get("candidate_symbols", []):
                c[s] += 1
        return {"items": [{"symbol": k, "count": n} for k, n in c.most_common(limit)]}

    @app.get("/ui/top-reasons")
    def ui_top_reasons(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        sup = _get_supervisor()
        rows = sup.storage.recent_records(limit=500, relevant_only=True)
        c = Counter()
        for r in rows:
            for s in r.get("impact_reason_codes", []):
                c[s] += 1
        return {"items": [{"reason": k, "count": n} for k, n in c.most_common(limit)]}

    @app.get("/ui/trends")
    def ui_trends(limit: int = Query(500, ge=10, le=5000)) -> dict[str, Any]:
        """Stacked trends across asset classes (sparkline-ready)."""
        sup = _get_supervisor()
        rows = sup.storage.recent_records(limit=limit, relevant_only=True)
        ts_ac: dict[str, Counter] = {}
        for r in rows:
            ts = r.get("published_ts") or r.get("created_at")
            if not ts:
                continue
            try:
                bucket = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:00")
            except ValueError:
                continue
            row = ts_ac.setdefault(bucket, Counter())
            for ac_ in r.get("asset_classes", []):
                row[ac_] += 1
        keys = sorted(ts_ac.keys())
        asset_classes = sorted({k for v in ts_ac.values() for k in v.keys()})
        series = {
            ac_: [ts_ac[k].get(ac_, 0) for k in keys]
            for ac_ in asset_classes
        }
        return {"buckets": keys, "asset_classes": asset_classes, "series": series}

    @app.get("/ui/matrix")
    def ui_matrix() -> dict[str, Any]:
        """Asset-class × reason-code co-occurrence matrix."""
        sup = _get_supervisor()
        rows = sup.storage.recent_records(limit=1000, relevant_only=True)
        cell: dict[tuple[str, str], int] = {}
        ac_set, rc_set = set(), set()
        for r in rows:
            for ac_ in r.get("asset_classes", []):
                ac_set.add(ac_)
                for rc_ in r.get("impact_reason_codes", []):
                    rc_set.add(rc_)
                    cell[(ac_, rc_)] = cell.get((ac_, rc_), 0) + 1
        acs = sorted(ac_set)
        rcs = sorted(rc_set)
        data = [[cell.get((ac_, rc_), 0) for rc_ in rcs] for ac_ in acs]
        return {"asset_classes": acs, "reason_codes": rcs, "matrix": data}

    @app.get("/ui/guards")
    def ui_guards() -> dict[str, Any]:
        s = _SETTINGS or load_settings()
        return _guard_snapshot(s)

    @app.get("/ui/benchmark/latest")
    def ui_benchmark_latest() -> dict[str, Any]:
        """Run the synthetic golden benchmark and return the report.

        This is intentionally synchronous and cheap (12 items, CPU stubs).
        """
        from .golden import SYNTHETIC, run_benchmark
        sup = _get_supervisor()
        rep = run_benchmark(sup.service, SYNTHETIC)
        return {**rep.to_dict(), "ran_at": datetime.now(timezone.utc).isoformat()}

    @app.get("/ui/benchmark/history")
    def ui_benchmark_history() -> dict[str, Any]:
        """Return the persisted benchmark history (if any). Empty for v1."""
        history_path = (_SETTINGS or load_settings()).paths.fusion_output_dir / "results" / "benchmark_history.jsonl"
        items: list[dict] = []
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return {"history": items[-50:]}

    @app.get("/ui/symbol/{symbol}")
    def ui_symbol(symbol: str, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        """Aggregate one symbol: records + per-reason and per-sentiment summary."""
        sup = _get_supervisor()
        items = sup.storage.by_label("symbol", symbol, limit=limit)
        rc, sent = Counter(), Counter()
        for r in items:
            for x in r.get("impact_reason_codes", []):
                rc[x] += 1
            if r.get("sentiment_label"):
                sent[r["sentiment_label"]] += 1
        return {
            "symbol": symbol,
            "count": len(items),
            "reason_distribution": dict(rc),
            "sentiment_distribution": dict(sent),
            "items": items,
        }

    @app.get("/ui/stream")
    async def ui_stream(request: Request) -> EventSourceResponse:
        """Server-Sent Events stream. Emits 'summary' periodically and a 'tick'
        heartbeat every 10s. Clients fall back to polling if SSE is blocked."""

        async def gen() -> AsyncIterator[dict[str, Any]]:
            last_total = -1
            last_emit = 0.0
            while True:
                if await request.is_disconnected():
                    return
                now = time.time()
                sup = _get_supervisor()
                counts = sup.storage.count_records()
                if counts["total"] != last_total or (now - last_emit) >= 30:
                    last_total = counts["total"]
                    last_emit = now
                    yield {
                        "event": "summary",
                        "data": json.dumps({
                            "totals": counts,
                            "dlq": sup.storage.dlq_count(),
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        }),
                    }
                else:
                    yield {"event": "tick", "data": str(int(now))}
                await asyncio.sleep(3.0)

        return EventSourceResponse(gen())

    return app


def _guard_snapshot(settings: Settings) -> dict[str, Any]:
    """Read-only guard status for the UI banner. Failure here never breaks UI."""
    try:
        snap = snapshot_guard_state(settings.paths.newsimpact_repo)
        return {
            "ok": True,
            "release_gate_passed": snap.release_gate_passed,
            "quarantine_state": snap.quarantine_state,
            "fusion_verdict_class": snap.fusion_verdict_class,
            "safe_to_publish": snap.safe_to_publish,
            "safe_to_promote": snap.safe_to_promote,
            "governance_index_sha256": snap.governance_index_sha256,
        }
    except NewsImpactGuardError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"unexpected: {exc}"}


def run() -> None:
    """Entry point for the ``fusion-stack-api`` console script."""
    s = load_settings()
    app = create_app(s)
    host = os.getenv("FUSION_API_HOST", s.api.host)
    port = int(os.getenv("FUSION_API_PORT", s.api.port))
    uvicorn.run(app, host=host, port=port, log_level=s.logging_.level.lower())


# Module-level app for `uvicorn fusion_stack.api:app`
app = create_app()
