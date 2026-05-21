"""FastAPI surface for the local catchem stack.

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
import os as _os_for_pid
import subprocess as _subproc
from datetime import datetime as _dt

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

_PROCESS_STARTED_AT = _dt.now(timezone.utc)

# Actual bind host/port observed at uvicorn startup, OR None if create_app
# was called without `record_bind` being invoked yet. Settings defaults
# (`s.api.host` / `s.api.port`) are NOT the truth — the CLI / Tauri shell
# can pin a different port via `--port` or env, and `/ui/sidecar-status`
# must report what we ACTUALLY bound, not what the config file says.
#
# Updated by `record_bind(host, port)` which the CLI calls right before
# `uvicorn.run(...)`. The Tauri shell hits `/ui/sidecar-status` to drive
# its connection details — surfacing a stale port from settings would
# mislead the operator the first time they ever changed the bind.
_BIND_HOST: str | None = None
_BIND_PORT: int | None = None


def record_bind(host: str, port: int) -> None:
    """Pin the host/port the process is actually serving on.

    Called from cli.py:serve() right before uvicorn.run().
    """
    global _BIND_HOST, _BIND_PORT
    _BIND_HOST = host
    _BIND_PORT = int(port)

from .contracts import (
    AppInfoResponse,
    DemoRunResponse,
    FinancialImpactDetail,
    FinancialImpactSummary,
    GuardSummary,
    LogTailResponse,
    MetricsSummary,
    RecordListResponse,
    SidecarStatusResponse,
)
from .archive import DriveArchiver
from .demo import DemoResult as _DemoResult
from .demo import build_capture as _build_capture, run_demo as _run_demo
from .text_extract import ALLOWED_SUFFIXES, MAX_UPLOAD_BYTES, extract_text
from .dashboard_data import overview
from .logging import get_logger
from .newsimpact_guarded_adapter import snapshot_guard_state, NewsImpactGuardError
from .news_poller import DEFAULT_FEEDS, FeedSpec, NewsPoller
from .redaction import redact_record_for_mode, redact_records_for_mode, safe_guard_view
from .schemas import AwarenessCaptureView
from .settings import Settings, load_settings
from .static_assets import get_static_path, open_static_bytes, static_dir
from .supervisor import Supervisor


def _is_production_safe() -> bool:
    s = _SETTINGS if _SETTINGS is not None else load_settings()
    return s.is_production_safe()


def _to_summary_list(items: list[dict[str, Any]], production_safe: bool) -> list[FinancialImpactSummary]:
    """Redact diagnostics first, then project to the compact summary contract."""
    redacted = redact_records_for_mode(items, production_safe=production_safe)
    return [FinancialImpactSummary.from_record_dict(r) for r in redacted]


def _normalize_detail_payload(r: dict[str, Any]) -> dict[str, Any]:
    """Coerce a storage row dict to FinancialImpactDetail input shape."""
    out = dict(r)
    for k in ("created_at", "published_ts"):
        v = out.get(k)
        if v is not None and not isinstance(v, str):
            out[k] = str(v)
    return out


def _git_sha_safe() -> str | None:
    """Resolve the current commit SHA without crashing if git is unavailable.

    Used by /ui/app-info; never blocks the response and never raises.
    """
    try:
        repo = Path(__file__).resolve().parents[2]
        res = _subproc.run(
            ["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def _git_branch_safe() -> str | None:
    try:
        repo = Path(__file__).resolve().parents[2]
        res = _subproc.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


logger = get_logger("catchem.api")

_SUPERVISOR: Supervisor | None = None
_SETTINGS: Settings | None = None
_NEWS_POLLER: NewsPoller | None = None
_ARCHIVER: DriveArchiver | None = None


def _get_supervisor() -> Supervisor:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        raise HTTPException(status_code=503, detail="supervisor_not_initialized")
    return _SUPERVISOR


def _build_news_poller(supervisor: Supervisor, settings: Settings) -> NewsPoller | None:
    """Construct the poller from settings, or None if disabled."""
    if not settings.news.poller_enabled:
        return None
    feeds: tuple[FeedSpec, ...] = DEFAULT_FEEDS
    if settings.news.feeds:
        feeds = tuple(
            FeedSpec(
                name=str(f.get("name", "user")),
                url=str(f["url"]),
                fallback_domain=str(f.get("fallback_domain", "")),
            )
            for f in settings.news.feeds
            if f.get("url")
        )
    return NewsPoller(
        supervisor=supervisor,
        settings=settings,
        feeds=feeds,
        interval_seconds=settings.news.poll_interval_seconds,
    )


def _build_archiver(supervisor: Supervisor, settings: Settings) -> DriveArchiver | None:
    """Construct the Drive archiver from settings, or None if disabled."""
    if not settings.archive.enabled:
        return None
    drive_dir: Path | None = None
    if settings.archive.drive_dir:
        drive_dir = Path(settings.archive.drive_dir).expanduser()
    return DriveArchiver(
        supervisor=supervisor,
        settings=settings,
        drive_dir=drive_dir,
        interval_seconds=settings.archive.interval_seconds,
        local_cap_rows=settings.archive.local_cap_rows,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    global _SUPERVISOR, _SETTINGS, _NEWS_POLLER, _ARCHIVER
    _SETTINGS = load_settings()
    _SUPERVISOR = Supervisor(_SETTINGS)
    _NEWS_POLLER = _build_news_poller(_SUPERVISOR, _SETTINGS)
    if _NEWS_POLLER is not None:
        _NEWS_POLLER.start()
    _ARCHIVER = _build_archiver(_SUPERVISOR, _SETTINGS)
    if _ARCHIVER is not None:
        _ARCHIVER.start()
    try:
        yield
    finally:
        if _ARCHIVER is not None:
            await _ARCHIVER.stop()
        _ARCHIVER = None
        if _NEWS_POLLER is not None:
            await _NEWS_POLLER.stop()
        _NEWS_POLLER = None
        if _SUPERVISOR is not None:
            _SUPERVISOR.close()
        _SUPERVISOR = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory. Tests can pass a Settings instance; CLI uses lifespan loading."""
    app = FastAPI(title="catchem", version="0.1.0", lifespan=lifespan)

    cors = (settings or Settings()).api.cors_origins
    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    # ── Conservative security headers ─────────────────────────────────────
    # Applied to every response. The CSP allows 'unsafe-inline' for both
    # style and script because:
    #   * the React shell injects a tiny inline script to set the theme class
    #     before paint (prevents FOUC),
    #   * the legacy dashboard ships its UI as an inline <script>.
    # Both inline scripts are author-controlled, version-pinned, and render
    # user-controlled data ONLY via textContent / React's text channel — never
    # via innerHTML-like APIs. The actual XSS protection lives in
    # `dashboard.html`'s `el()` helper and React's JSX escaping, not in CSP.
    # If you ever switch to nonce-based CSP, drop the 'unsafe-inline' tokens.
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # ── Legacy vanilla dashboard (kept until the premium app fully replaces it)
    @app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/legacy-dashboard", response_class=HTMLResponse, include_in_schema=False)
    def legacy() -> HTMLResponse:
        body = open_static_bytes("dashboard.html")
        if body is None:
            return HTMLResponse("<h1>dashboard template missing</h1>", status_code=404)
        return HTMLResponse(body.decode("utf-8"))

    # ── Premium SPA bundle served at /
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root() -> HTMLResponse:
        body = open_static_bytes("app/index.html")
        if body is not None:
            return HTMLResponse(body.decode("utf-8"))
        # Friendly fallback when the bundle hasn't been built yet
        msg = (
            "<!doctype html><meta charset=utf-8><title>catchem</title>"
            "<style>body{font-family:ui-monospace,monospace;background:#0e1014;color:#e7ebf0;"
            "padding:48px;max-width:640px;line-height:1.6}h1{color:#5fb3ff;font-size:18px}"
            "code{background:#161922;padding:2px 6px;border-radius:4px}a{color:#5fb3ff}</style>"
            "<h1>catchem</h1>"
            "<p>The premium UI bundle has not been built yet.</p>"
            "<p>Run <code>bash scripts/catchem_bootstrap_and_run.sh</code> "
            "or <code>(cd frontend && npm install && npm run build)</code>.</p>"
            "<p>Legacy dashboard meanwhile: <a href=\"/legacy\">/legacy</a></p>"
        )
        return HTMLResponse(msg, status_code=200)

    # Mount the built bundle's static assets if they exist.
    # We resolve via the same package-resource helper so wheel installs work.
    _assets_root = get_static_path("app/index.html")
    if _assets_root is not None:
        _assets_dir = _assets_root.parent / "assets"
        if _assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        ico = get_static_path("app/favicon.ico")
        if ico is not None and ico.exists():
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
        status = sup.status()
        # In production_safe mode diagnostic must read False even if a future
        # bug flipped supervisor state mid-flight.
        if _is_production_safe():
            status["diagnostic_enabled"] = False
        # Surface a stable contract for downstream consumers.
        status.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
        return status

    @app.get("/recent", response_model=RecordListResponse)
    def recent(limit: int = Query(50, ge=1, le=500), relevant_only: bool = True) -> RecordListResponse:
        sup = _get_supervisor()
        items = sup.storage.recent_records(limit=limit, relevant_only=relevant_only)
        return RecordListResponse(items=_to_summary_list(items, _is_production_safe()))

    @app.get("/dashboard")
    def dashboard(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        sup = _get_supervisor()
        out = overview(sup.storage, limit=limit)
        if _is_production_safe():
            out["recent"] = redact_records_for_mode(out.get("recent", []), production_safe=True)
            out["diagnostic_count"] = 0
        return out

    @app.get("/record/{capture_id}", response_model=FinancialImpactDetail)
    def record(capture_id: str) -> FinancialImpactDetail:
        sup = _get_supervisor()
        rec = sup.storage.get_record(capture_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="capture_not_found")
        redacted = redact_record_for_mode(rec, production_safe=_is_production_safe()) or {}
        return FinancialImpactDetail(**_normalize_detail_payload(redacted))

    @app.get("/records/by-symbol/{symbol}", response_model=RecordListResponse)
    def by_symbol(symbol: str, limit: int = Query(50, ge=1, le=500)) -> RecordListResponse:
        sup = _get_supervisor()
        items = sup.storage.by_label("symbol", symbol, limit=limit)
        return RecordListResponse(items=_to_summary_list(items, _is_production_safe()))

    @app.get("/records/by-asset-class/{asset_class}", response_model=RecordListResponse)
    def by_asset_class(asset_class: str, limit: int = Query(50, ge=1, le=500)) -> RecordListResponse:
        sup = _get_supervisor()
        items = sup.storage.by_label("asset_class", asset_class, limit=limit)
        return RecordListResponse(items=_to_summary_list(items, _is_production_safe()))

    @app.get("/records/by-reason/{reason_code}", response_model=RecordListResponse)
    def by_reason(reason_code: str, limit: int = Query(50, ge=1, le=500)) -> RecordListResponse:
        sup = _get_supervisor()
        items = sup.storage.by_label("reason_code", reason_code, limit=limit)
        return RecordListResponse(items=_to_summary_list(items, _is_production_safe()))

    @app.post("/replay")
    def replay(max_records: int = Body(50, embed=True)) -> dict[str, Any]:
        sup = _get_supervisor()
        return sup.run_replay(max_records=max_records)

    @app.post("/process-one", response_model=FinancialImpactDetail)
    def process_one(capture: dict = Body(...)) -> FinancialImpactDetail:
        sup = _get_supervisor()
        cap = AwarenessCaptureView.model_validate(capture)
        rec = sup.process_capture(cap)
        payload = rec.model_dump(mode="json")
        redacted = redact_record_for_mode(payload, production_safe=_is_production_safe()) or payload
        return FinancialImpactDetail(**_normalize_detail_payload(redacted))

    # ────────────────────────────────────────────────────────────────────────
    # Catchem desktop endpoints — typed wrappers around demo.py + safe upload.
    # All preserve the production-safe redaction guarantees.
    # ────────────────────────────────────────────────────────────────────────

    def _demo_to_response(result: "_DemoResult") -> DemoRunResponse:
        prod_safe = _is_production_safe()
        rec = redact_record_for_mode(result.record, production_safe=prod_safe) or {}
        if not rec:
            # No record materialized — surface a stub-shaped detail so the
            # response_model still validates, but mark it inert.
            rec = {
                "capture_id": result.capture_id,
                "doc_id": f"demo-{result.capture_id}",
                "is_finance_relevant": False,
                "finance_relevance_score": 0.0,
                "processing_mode": "production_safe" if prod_safe else "research_diagnostic",
                "created_at": _dt.now(timezone.utc).isoformat(),
                "title": None,
            }
        return DemoRunResponse(
            capture_id=result.capture_id,
            jsonl_basename=Path(str(result.jsonl_path)).name,
            processed=result.processed,
            skipped=result.skipped,
            record=FinancialImpactDetail(**_normalize_detail_payload(rec)),
        )

    @app.post("/ui/demo/paste", response_model=DemoRunResponse)
    def ui_demo_paste(body: dict = Body(...)) -> DemoRunResponse:
        """Paste a news article → demo pipeline → typed record."""
        title = str(body.get("title") or "").strip()
        text = str(body.get("text") or "").strip()
        domain = str(body.get("domain") or "demo.local").strip() or "demo.local"
        url = body.get("url") or None
        if not title or not text:
            raise HTTPException(status_code=422, detail="title and text are required")
        if len(text) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="text exceeds size cap")
        result = _run_demo(title=title, text=text, domain=domain, url=url)
        return _demo_to_response(result)

    @app.post("/ui/demo/upload", response_model=DemoRunResponse)
    async def ui_demo_upload(
        file: UploadFile = File(...),
        title: str | None = Form(None),
        domain: str = Form("demo.local"),
        url: str | None = Form(None),
    ) -> DemoRunResponse:
        """Upload .txt/.md/.html/.jsonl/.json → safe text extract → demo pipeline."""
        body = await file.read()
        try:
            title_hint, body_text = extract_text(file.filename or "upload", body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        effective_title = (title or title_hint or "(untitled upload)").strip()
        result = _run_demo(title=effective_title, text=body_text, domain=domain or "demo.local", url=url)
        return _demo_to_response(result)

    @app.get("/ui/app-info", response_model=AppInfoResponse)
    def ui_app_info() -> AppInfoResponse:
        from . import __version__ as _ver
        s = _SETTINGS or load_settings()
        sup = _get_supervisor()
        commit = _git_sha_safe()
        branch = _git_branch_safe()
        bundle = get_static_path("app/index.html")
        return AppInfoResponse(
            version=_ver,
            commit_sha=commit,
            branch=branch,
            mode=s.mode.value,
            use_ml_stubs=s.models_.use_ml_stubs,
            diagnostic_allowed=s.diagnostic_allowed(),
            static_bundle_present=bundle is not None,
            model_versions=dict(sup.service.model_versions),
            generated_at=_dt.now(timezone.utc).isoformat(),
        )

    @app.get("/ui/sidecar-status", response_model=SidecarStatusResponse)
    def ui_sidecar_status() -> SidecarStatusResponse:
        sup = _get_supervisor()
        s = _SETTINGS or load_settings()
        counts = sup.storage.count_records()
        uptime = (_dt.now(timezone.utc) - _PROCESS_STARTED_AT).total_seconds()
        return SidecarStatusResponse(
            healthy=True,
            # Prefer the actual bind recorded at startup over the static
            # settings value. Without this the UI claims `:8087` even when
            # the operator launched with `--port 9090`.
            api_host=_BIND_HOST if _BIND_HOST is not None else s.api.host,
            api_port=_BIND_PORT if _BIND_PORT is not None else s.api.port,
            pid=_os_for_pid.getpid(),
            uptime_seconds=uptime,
            records=counts,
            dlq=sup.storage.dlq_count(),
            diagnostic_enabled=False if _is_production_safe() else s.diagnostic_allowed(),
            generated_at=_dt.now(timezone.utc).isoformat(),
        )

    @app.get("/ui/log-tail", response_model=LogTailResponse)
    def ui_log_tail(lines: int = Query(120, ge=1, le=2000)) -> LogTailResponse:
        s = _SETTINGS or load_settings()
        log_rel = s.logging_.file
        # `file` is relative like "data/logs/catchem.log"; resolve under output dir
        if log_rel.startswith("data/"):
            log_path = s.paths.catchem_output_dir / Path(log_rel).relative_to("data")
        else:
            log_path = Path(log_rel)
        if not log_path.exists():
            return LogTailResponse(lines=[], truncated=False)
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return LogTailResponse(lines=[], truncated=False)
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
        return LogTailResponse(lines=tail, truncated=len(all_lines) > len(tail))

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
        prod_safe = s.is_production_safe()
        recent_top = dash["recent"][:6]
        return {
            "mode": s.mode.value,
            "is_production_safe": prod_safe,
            "diagnostic_allowed": s.diagnostic_allowed(),
            "use_ml_stubs": s.models_.use_ml_stubs,
            "totals": dash["totals"],
            "diagnostic_count": 0 if prod_safe else dash["diagnostic_count"],
            "asset_class_distribution": dash["asset_class_distribution"],
            "reason_code_distribution": dash["reason_code_distribution"],
            "sentiment_distribution": dash["sentiment_distribution"],
            "recent_top": redact_records_for_mode(recent_top, production_safe=prod_safe),
            "dlq": sup.storage.dlq_count(),
            "model_versions": dict(sup.service.model_versions),
            "guards": safe_guard_view(guards),
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
        return safe_guard_view(_guard_snapshot(s))

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
        history_path = (_SETTINGS or load_settings()).paths.catchem_output_dir / "results" / "benchmark_history.jsonl"
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
            "items": redact_records_for_mode(items, production_safe=_is_production_safe()),
        }

    @app.get("/ui/news-status")
    def news_status() -> dict[str, Any]:
        """Diagnostics for the background RSS poller. Surfaced in Live Feed UI."""
        if _NEWS_POLLER is None:
            return {
                "enabled": False,
                "feeds": 0,
                "interval_seconds": None,
                "last_run_at": None,
                "next_run_at": None,
                "last_ingested": 0,
                "total_ingested": 0,
                "last_error": None,
                "is_polling": False,
                "last_new_at": None,
                "empty_ticks": 0,
                "last_avg_publisher_lag_seconds": None,
                "last_median_publisher_lag_seconds": None,
                "unhealthy_feeds": 0,
                "feed_health": [],
                "max_item_age_seconds": None,
                "last_stale_skipped": 0,
            }
        feed_health = _NEWS_POLLER.feed_health_snapshot()
        return {
            "enabled": True,
            "feeds": len(_NEWS_POLLER._feeds),  # type: ignore[attr-defined]
            "interval_seconds": _NEWS_POLLER._interval,  # type: ignore[attr-defined]
            "last_run_at": _NEWS_POLLER.last_run_at.isoformat() if _NEWS_POLLER.last_run_at else None,
            "next_run_at": _NEWS_POLLER.next_run_at.isoformat() if _NEWS_POLLER.next_run_at else None,
            "last_ingested": _NEWS_POLLER.last_ingested,
            "total_ingested": _NEWS_POLLER.total_ingested,
            "last_error": _NEWS_POLLER.last_error,
            "is_polling": _NEWS_POLLER.is_polling,
            # Distinguishes "actively flowing" from "alive but quiet" —
            # the UI uses these to show "last new arrival: X min ago" when
            # last_ingested has been 0 for several ticks. Reassures the
            # analyst the poller is healthy even when publishers are idle.
            "last_new_at": _NEWS_POLLER.last_new_at.isoformat() if _NEWS_POLLER.last_new_at else None,
            "empty_ticks": _NEWS_POLLER.empty_ticks,
            # Average/median seconds between item.published_ts and ingest
            # time, over the most recent poll. Lets the UI explicitly show
            # the analyst how much of the visible lag is publisher-side
            # vs our pipeline (our pipeline is ~4ms/item in stub mode).
            "last_avg_publisher_lag_seconds": _NEWS_POLLER.last_avg_publisher_lag_seconds,
            "last_median_publisher_lag_seconds": _NEWS_POLLER.last_median_publisher_lag_seconds,
            "unhealthy_feeds": sum(1 for f in feed_health if int(f.get("consecutive_errors") or 0) > 0),
            "feed_health": feed_health,
            "max_item_age_seconds": _NEWS_POLLER._max_item_age_seconds,  # type: ignore[attr-defined]
            "last_stale_skipped": _NEWS_POLLER.last_stale_skipped,
        }

    @app.post("/ui/news-poll-now")
    async def news_poll_now() -> dict[str, Any]:
        """Force an immediate news-poll tick. Powers the UI 'Poll now' button.

        Idempotent under concurrent calls — the poller's internal lock
        serializes runs, so two clicks become one ingest + one no-op.
        """
        if _NEWS_POLLER is None:
            raise HTTPException(status_code=503, detail="news_poller_disabled")
        ingested = await _NEWS_POLLER.poll_now()
        return {"ingested": ingested, "total_ingested": _NEWS_POLLER.total_ingested}

    @app.get("/ui/archive-status")
    def archive_status() -> dict[str, Any]:
        """Diagnostics for the Drive archiver."""
        if _ARCHIVER is None:
            return {
                "enabled": False,
                "drive_dir": None,
                "interval_seconds": None,
                "local_cap_rows": None,
                "last_run_at": None,
                "last_archived_count": 0,
                "total_archived": 0,
                "last_error": None,
                "is_archiving": False,
                "current_csv_path": None,
            }
        return {
            "enabled": True,
            "drive_dir": str(_ARCHIVER.drive_dir),
            "interval_seconds": _ARCHIVER.interval_seconds,
            "local_cap_rows": _ARCHIVER.local_cap,
            "last_run_at": _ARCHIVER.last_run_at.isoformat() if _ARCHIVER.last_run_at else None,
            "last_archived_count": _ARCHIVER.last_archived_count,
            "total_archived": _ARCHIVER.total_archived,
            "last_error": _ARCHIVER.last_error,
            "is_archiving": _ARCHIVER.is_archiving,
            "current_csv_path": str(_ARCHIVER.current_csv_path) if _ARCHIVER.current_csv_path else None,
        }

    @app.post("/ui/archive-now")
    async def archive_now() -> dict[str, Any]:
        """Force an immediate archive sweep. Powers the UI 'Archive now' button."""
        if _ARCHIVER is None:
            raise HTTPException(status_code=503, detail="archiver_disabled")
        result = await _ARCHIVER.archive_now()
        return {
            "archived": result.archived,
            "csv_path": str(result.csv_path) if result.csv_path else None,
            "error": result.error,
            "total_archived": _ARCHIVER.total_archived,
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

    # ── SPA history-mode fallback ─────────────────────────────────────────
    # React Router uses history-mode URLs (/replay, /model-controls, /help).
    # When the browser asks the server directly for one of those (bookmark,
    # refresh, deep-link), FastAPI must serve the bundle shell so the SPA
    # can boot and route client-side. We only fall back for GET requests
    # that don't already match an API/assets route, and we never shadow the
    # /assets mount or /docs (OpenAPI).
    _RESERVED_PATH_PREFIXES = (
        "assets/", "docs", "openapi.json", "redoc",
        "healthz", "config", "metrics", "recent", "record",
        "records/", "process-one", "dashboard",
        "legacy", "ui/", "favicon",
        # NB: "replay" deliberately not reserved — both the SPA route
        # (handled by replay_spa) and the POST API live on /replay.
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> HTMLResponse:
        # Reserved API/asset paths must NOT fall back to HTML — those that
        # don't match a real handler should return their natural 404/405.
        for prefix in _RESERVED_PATH_PREFIXES:
            if full_path == prefix.rstrip("/") or full_path.startswith(prefix):
                raise HTTPException(status_code=404, detail="not_found")
        body = open_static_bytes("app/index.html")
        if body is not None:
            return HTMLResponse(body.decode("utf-8"))
        raise HTTPException(status_code=404, detail="bundle_not_built")

    # The Catchem nav routes /replay → ReplayUploadPage. The existing
    # POST /replay endpoint stays for API consumers, but a bookmarked GET
    # to /replay must serve the SPA shell. Explicit handler avoids the
    # 405-before-fallback case.
    @app.get("/replay", response_class=HTMLResponse, include_in_schema=False)
    def replay_spa() -> HTMLResponse:
        body = open_static_bytes("app/index.html")
        if body is not None:
            return HTMLResponse(body.decode("utf-8"))
        raise HTTPException(status_code=404, detail="bundle_not_built")

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
    """Entry point for the ``catchem-api`` console script."""
    s = load_settings()
    app = create_app(s)
    host = os.getenv("CATCHEM_API_HOST", s.api.host)
    port = int(os.getenv("CATCHEM_API_PORT", s.api.port))
    uvicorn.run(app, host=host, port=port, log_level=s.logging_.level.lower())


# Module-level app for `uvicorn catchem.api:app`
app = create_app()
