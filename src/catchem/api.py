"""FastAPI surface for the local catchem stack.

Endpoints are intentionally thin pass-throughs to the Supervisor. Auth is
out-of-scope (local-first). The API binds to 127.0.0.1 by default.
"""
# ruff: noqa: E402, B008
#
# E402 noqa: the cross-module imports below the `record_bind` definition
# need that function to exist at import time (sidecar.rs reads the
# module-level `_BIND_HOST` / `_BIND_PORT` via the same module) — we
# can't push the imports to the very top without splitting the bind
# state into a separate module, which would fragment the API surface
# for no real win.
#
# B008 noqa: the `Body(...)` / `File(...)` / `Form(...)` calls in
# parameter defaults are the FastAPI idiom — they declare metadata
# for FastAPI's dependency-injection layer, not a default Python
# value. Moving them inside the function body would break the API.

from __future__ import annotations

import asyncio
import json
import os
import os as _os_for_pid
import re
import secrets
import subprocess as _subproc
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

_PROCESS_STARTED_AT = _dt.now(UTC)
# Alias kept for /api/stats — the spec refers to the supervisor's
# start as `_SUPERVISOR_START_TIME` while the rest of the module already
# uses `_PROCESS_STARTED_AT`. They are the same wall-clock moment
# (module load = uvicorn boot = supervisor lifespan entry) so a single
# variable backs both names.
_SUPERVISOR_START_TIME = _PROCESS_STARTED_AT

# Per-path request counter populated by the `_request_counter` middleware.
# Keyed by the matched route template (e.g. "/api/stats") falling back to
# the raw URL path when no match exists. Read by /api/stats; never reset
# at runtime (a fresh process gets fresh counts, which is the right grain
# for a local-first cockpit).
_REQUEST_COUNTS: dict[str, int] = {}

# Tiny TTL cache for the /api/stats payload. The DB COUNT() calls are
# cheap on small tables but the endpoint is polled every 5s by the Ops
# card — caching for 2s gives multi-window cockpits a stampede-resistant
# hot path without ever showing stale-by-more-than-2s data.
#
# Concurrency: FastAPI / uvicorn dispatches the sync handler to a
# threadpool, so two concurrent /api/stats requests can land on two
# different worker threads. Without ``_STATS_CACHE_LOCK`` both threads
# could observe ``cached is None`` and both run the DB COUNT() phase,
# defeating the cache and (worse) creating a brief window where a
# third reader sees a half-populated dict (Python's dict ops are
# atomic at the level of any single read, but ``payload`` and
# ``expires_at`` are written separately so a reader between the two
# writes sees a fresh payload with the OLD expires_at — effectively
# expiring the new payload instantly).
#
# The lock is held only across the cache miss path; cache hits read
# under the lock too because GIL-atomicity does not guarantee that a
# .get("payload") + .get("expires_at") pair observes the same write
# generation. The lock window is well below 1 ms in the hit path and
# well below 50 ms in the miss path (3× SQLite COUNT() against tables
# typically <50k rows), so there is no realistic risk of starving
# other handlers.
_STATS_CACHE: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_STATS_CACHE_LOCK = threading.Lock()
_STATS_TTL_SECONDS = 2.0

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


# ── Nonce-based CSP plumbing ────────────────────────────────────────────────
# The SPA shell ships ONE inline <script> (the FOUC-preventing theme bootstrap
# in `static/app/index.html`). To avoid `'unsafe-inline'` on script-src we
# stamp a per-request nonce onto that inline tag and emit a matching
# `script-src 'self' 'nonce-<random>'` header. The ESM bundle is loaded via
# `<script type="module" src="/assets/...">` and remains permitted by 'self'
# without a nonce — but we still attach one (browsers ignore the nonce when
# `src` is present, but explicit is safer).
#
# `_INLINE_SCRIPT_RE` matches any `<script>` tag that does NOT already carry
# a `nonce=` attribute. The negative-lookahead on `nonce=` makes the
# substitution idempotent (re-running it on already-stamped HTML is a no-op),
# which keeps unit tests stable when the cache is reused across requests.
# The pattern intentionally rewrites BOTH inline and src-bearing <script>
# tags — the ESM module also gets the nonce so a future tightened CSP that
# requires `'strict-dynamic'` won't need another pass.
_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bnonce=)([^>]*)>",
    re.IGNORECASE,
)


def _csp_with_nonce(nonce: str) -> str:
    """The full Content-Security-Policy for SPA HTML responses.

    Drops `'unsafe-inline'` from script-src in favour of a nonce. Style-src
    keeps `'unsafe-inline'` because React inlines style attributes on
    elements (e.g. dynamic theme variables on the root); a nonce-only style
    policy would also need to cover every `style={...}` JSX prop, which
    React does not currently surface a hook for.
    """
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


def _render_spa_with_nonce() -> tuple[str | None, str]:
    """Load the SPA shell, stamp a fresh nonce on every <script> tag.

    Returns `(html, nonce)`. When the bundle hasn't been built yet, `html`
    is None and the caller is expected to fall back to the placeholder page.
    A fresh nonce is generated per call (16 random bytes → 22-char urlsafe
    base64) so that even a CDN that ignores `Cache-Control: no-store`
    cannot serve a stale page with a guessable token.
    """
    body = open_static_bytes("app/index.html")
    if body is None:
        return None, secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    html = body.decode("utf-8")
    # Stamp the nonce onto every <script> tag that doesn't already have one.
    # Backreference \1 preserves the rest of the tag's attributes (src,
    # type=module, crossorigin, etc.) verbatim.
    html_with_nonce = _INLINE_SCRIPT_RE.sub(
        rf'<script nonce="{nonce}"\1>',
        html,
    )
    return html_with_nonce, nonce


from .archive import DriveArchiver
from .contracts import (
    AppInfoResponse,
    DemoRunResponse,
    FinancialImpactDetail,
    FinancialImpactSummary,
    LogTailResponse,
    MarketQuote,
    MarketQuoteBatchResponse,
    RecordListResponse,
    SidecarStatusResponse,
)
from .dashboard_data import overview
from .demo import DemoResult as _DemoResult
from .demo import run_demo as _run_demo
from .logging import get_logger
from .market_data import LocalFixtureMarketDataProvider, parse_symbol_list
from .news_poller import DEFAULT_FEEDS, FeedSpec, NewsPoller, assemble_feeds
from .newsimpact_guarded_adapter import NewsImpactGuardError, snapshot_guard_state
from .rate_limit import (
    DB_IMPORT_BUCKET,
    DEFAULT_BUCKET,
    SEARCH_BUCKET,
    check_rate,
)
from .redaction import redact_record_for_mode, redact_records_for_mode, safe_guard_view
from .runtime_metrics import current_metrics as _current_process_metrics
from .schemas import AwarenessCaptureView
from .settings import Settings, load_settings
from .static_assets import get_static_path, open_static_bytes
from .supervisor import Supervisor
from .text_extract import MAX_UPLOAD_BYTES, extract_text


def _is_production_safe() -> bool:
    s = _SETTINGS if _SETTINGS is not None else load_settings()
    return s.is_production_safe()


def _display_path(p: Path | str | None) -> str | None:
    """Redact an absolute path to a tilde-relative form for UI surfaces.

    `/Users/nazmi/Documents/Catchem` becomes `~/Documents/Catchem`, which is
    clear to the operator but does not leak the local username to anyone
    glancing at the screen (or to UI tooltips persisted in logs/screenshots).
    Paths outside $HOME (e.g. `/tmp/...` in tests, network mounts) are
    returned as-is — they don't carry user-identifying segments.
    """
    if p is None:
        return None
    text = str(p)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return text
    if home and text == home:
        return "~"
    if home and text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text


# Regulator / official-source domains. A feed whose fallback_domain matches one
# of these (or a subdomain of one) is classified as "regulator" regardless of
# its name prefix, so e.g. a plain RSS feed off federalreserve.gov is not
# mis-bucketed as "wire". Kept as a frozenset for O(1) suffix checks.
_REGULATOR_DOMAINS: frozenset[str] = frozenset(
    {
        "federalreserve.gov",
        "sec.gov",
        "ecb.europa.eu",
        "bankofengland.co.uk",
        "imf.org",
        "worldbank.org",
        "bis.org",
        "treasury.gov",
        "cftc.gov",
        "finra.org",
        "esma.europa.eu",
        "bankofcanada.ca",
        "rba.gov.au",
        "boj.or.jp",
    }
)

# Crypto-native publishers. Same idea as the regulator set: a feed whose
# fallback_domain matches (or is a subdomain of) one of these is "crypto" even
# when it carries no recognizable name prefix.
_CRYPTO_DOMAINS: frozenset[str] = frozenset(
    {
        "coindesk.com",
        "cointelegraph.com",
        "theblock.co",
        "decrypt.co",
        "bitcoinmagazine.com",
        "cryptoslate.com",
        "blockworks.co",
    }
)


def _domain_matches(domain: str, known: frozenset[str]) -> bool:
    """True when `domain` equals or is a subdomain of any entry in `known`.

    `www.sec.gov` and `sec.gov` both match a `sec.gov` entry; `notsec.gov`
    does not (suffix is compared on a dot boundary). Empty domain → False.
    """
    d = (domain or "").strip().lower()
    if not d:
        return False
    return any(d == k or d.endswith("." + k) for k in known)


def _classify_feed_category(name: str, parser: str, domain: str) -> str:
    """Bucket one configured feed into a coverage category for the operator.

    Pure + deterministic so it can be unit-tested in isolation. Resolution
    order is *most specific first*:

      1. Name prefixes that disambiguate Google News sub-streams
         (``gnews-watch-`` → watchlist, ``gnews-tkr-`` → tickers) before the
         generic ``gnews-`` → google_news catch.
      2. Parser-driven social/firehose buckets (twitter, reddit, gdelt) and the
         matching ``x-`` / ``reddit-`` name prefixes.
      3. Remaining domain-flavored name prefixes (regional/macro/specialist/
         video/podcast).
      4. Domain-set membership (regulator, then crypto).
      5. Fallback: ``wire`` (generic newswire).
    """
    n = (name or "").lower()
    p = (parser or "").lower()

    # 1 — Google News sub-streams (specific prefixes win over generic).
    if n.startswith("gnews-watch-"):
        return "watchlist"
    if n.startswith("gnews-tkr-"):
        return "tickers"
    if n.startswith("gnews-"):
        return "google_news"

    # 2 — Social + global firehose (parser OR name prefix).
    if p == "twitter" or n.startswith("x-"):
        return "social"
    if p == "reddit" or n.startswith("reddit-"):
        return "social"
    if p == "gdelt":
        return "global_firehose"

    # 3 — Domain-flavored prefixes.
    if n.startswith("rem-"):
        return "regional"
    if n.startswith("macro-"):
        return "macro"
    if n.startswith("spec-"):
        return "specialist"
    if n.startswith("yt-"):
        return "video"
    if n.startswith("pod-"):
        return "podcast"

    # 4 — Official / crypto publishers by domain (or explicit name membership).
    if n in _REGULATOR_DOMAINS or _domain_matches(domain, _REGULATOR_DOMAINS):
        return "regulator"
    if _domain_matches(domain, _CRYPTO_DOMAINS):
        return "crypto"

    # 5 — Generic newswire.
    return "wire"


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


def _compute_agreement(stub_payload: dict[str, Any], ds_payload: dict[str, Any]) -> dict[str, Any]:
    """Pairwise agreement scoring between stub + DeepSeek review payloads.

    Returns a dict with per-field scores so the UI can render an
    agreement matrix. The numeric fields are intentionally simple — the
    point is to *surface* disagreement, not crown a winner.

    Scores:
      * `relevance_match`   — bool, did both call it finance-relevant?
      * `score_delta`       — abs difference between relevance scores
      * `asset_jaccard`     — Jaccard index over asset_classes (0-1)
      * `reason_jaccard`    — same over impact_reason_codes
      * `symbol_jaccard`    — same over candidate_symbols
      * `sentiment_match`   — bool, same sentiment_label?
      * `overall`           — equal-weight mean of the bounded fields
    """
    def _jaccard(a: list[str] | None, b: list[str] | None) -> float:
        sa, sb = set(a or []), set(b or [])
        if not sa and not sb:
            return 1.0
        union = sa | sb
        if not union:
            return 1.0
        return round(len(sa & sb) / len(union), 4)

    relevance_match = bool(stub_payload.get("is_finance_relevant")) == bool(
        ds_payload.get("is_finance_relevant")
    )
    score_delta = round(
        abs(
            float(stub_payload.get("finance_relevance_score") or 0.0)
            - float(ds_payload.get("finance_relevance_score") or 0.0)
        ),
        4,
    )
    asset_jaccard = _jaccard(stub_payload.get("asset_classes"), ds_payload.get("asset_classes"))
    reason_jaccard = _jaccard(
        stub_payload.get("impact_reason_codes"), ds_payload.get("impact_reason_codes")
    )
    symbol_jaccard = _jaccard(
        stub_payload.get("candidate_symbols"), ds_payload.get("candidate_symbols")
    )
    sentiment_match = (
        stub_payload.get("sentiment_label") is not None
        and stub_payload.get("sentiment_label") == ds_payload.get("sentiment_label")
    )
    # Equal-weight aggregate so the compare page's lead number isn't
    # dominated by any one field. Score-delta inverted because lower is
    # better. All six bounded fields participate (docstring says "the
    # bounded fields" — symbol_jaccard belongs in that set too, and was
    # silently dropped from the average before).
    overall = round(
        (
            (1.0 if relevance_match else 0.0)
            + (1.0 - min(1.0, score_delta))
            + asset_jaccard
            + reason_jaccard
            + symbol_jaccard
            + (1.0 if sentiment_match else 0.0)
        )
        / 6.0,
        4,
    )
    return {
        "relevance_match": relevance_match,
        "score_delta": score_delta,
        "asset_jaccard": asset_jaccard,
        "reason_jaccard": reason_jaccard,
        "symbol_jaccard": symbol_jaccard,
        "sentiment_match": sentiment_match,
        "overall": overall,
    }


def _compute_compare_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-row agreement into a single dashboard summary."""
    if not items:
        return {
            "n": 0,
            "relevance_match_rate": 0.0,
            "sentiment_match_rate": 0.0,
            "mean_asset_jaccard": 0.0,
            "mean_reason_jaccard": 0.0,
            "mean_symbol_jaccard": 0.0,
            "mean_score_delta": 0.0,
            "mean_overall": 0.0,
            "deepseek_errors": 0,
        }
    n = len(items)
    rel = sum(1 for it in items if it["agreement"]["relevance_match"])
    sent = sum(1 for it in items if it["agreement"]["sentiment_match"])
    asset = sum(it["agreement"]["asset_jaccard"] for it in items)
    reason = sum(it["agreement"]["reason_jaccard"] for it in items)
    symbol = sum(it["agreement"]["symbol_jaccard"] for it in items)
    score_delta = sum(it["agreement"]["score_delta"] for it in items)
    overall = sum(it["agreement"]["overall"] for it in items)
    errors = sum(1 for it in items if it["deepseek"].get("error_code"))
    return {
        "n": n,
        "relevance_match_rate": round(rel / n, 4),
        "sentiment_match_rate": round(sent / n, 4),
        "mean_asset_jaccard": round(asset / n, 4),
        "mean_reason_jaccard": round(reason / n, 4),
        "mean_symbol_jaccard": round(symbol / n, 4),
        "mean_score_delta": round(score_delta / n, 4),
        "mean_overall": round(overall / n, 4),
        "deepseek_errors": errors,
    }


def _local_explain(kind: str, payload: dict[str, Any]) -> str:
    """Deterministic fallback narrative for quant signals (no LLM needed).

    Used when DeepSeek is unavailable or the budget cap is hit. Returns a
    short, factual sentence built from the payload — never crashes, never
    makes things up.
    """
    if kind == "cluster":
        size = payload.get("size", 0)
        domains = payload.get("member_domains") or []
        symbols = (payload.get("dominant_symbols") or [])[:3]
        reasons = (payload.get("dominant_reasons") or [])[:2]
        return (
            f"Cluster of {size} captures across {len(domains)} sources"
            + (f" — focused on {', '.join(symbols)}" if symbols else "")
            + (f" via {', '.join(reasons)}" if reasons else "")
            + "."
        )
    if kind == "regime_shift":
        kl = payload.get("kl_divergence_from_prev")
        ts = payload.get("bucket_start", "")
        top_assets = (payload.get("asset_distribution") or [])[:2]
        a_str = ", ".join(f"{k} {v:.0%}" if isinstance(v, (int, float)) else str(k) for k, v in top_assets) if top_assets else ""
        return (
            f"Topic mix shifted at {ts} (KL={kl:.2f})" if isinstance(kl, (int, float))
            else f"Topic mix shifted at {ts}"
        ) + (f"; now dominated by {a_str}" if a_str else "") + "."
    if kind == "anomaly":
        z = payload.get("z_score", 0)
        observed = payload.get("observed")
        sym = payload.get("symbol")
        if sym:
            return f"Symbol burst on {sym}: {observed} mentions, z-score {z:.1f}."
        return f"Volume/sentiment anomaly: observed={observed}, z-score {z:.1f}."
    if kind == "spillover":
        src = payload.get("source_asset")
        tgt = payload.get("target_asset")
        score = payload.get("spillover_score", 0)
        return f"Spillover detected: {src} → {tgt} (score {score:.2f})."
    return "Quant signal flagged; no specific local interpretation available."


def _local_live_read(ctx: dict[str, Any]) -> str:
    """Deterministic fallback narrative for the /api/quant/live-read endpoint.

    Builds 2-3 sentences from the snapshot context without an LLM. Used when
    DeepSeek is disabled or the budget cap is hit so the hero is never blank.
    """
    parts: list[str] = []
    n = ctx.get("window_records") or 0
    clusters = ctx.get("clusters_active") or 0
    shifts = ctx.get("regime_shifts_recent") or 0
    parts.append(f"{n} records in window across {clusters} active cluster{'s' if clusters != 1 else ''}.")
    bursts = ctx.get("symbol_bursts") or []
    vols = ctx.get("volume_anomalies") or 0
    shocks = ctx.get("sentiment_shocks") or 0
    if bursts:
        top = bursts[0]
        parts.append(
            f"Symbol burst on {top.get('symbol')} ({top.get('observed')} mentions, z={top.get('z') or 0:.1f}); "
            f"{vols} volume / {shocks} sentiment anomalies otherwise."
        )
    elif vols + shocks > 0:
        parts.append(f"{vols} volume / {shocks} sentiment anomalies firing; no symbol burst.")
    else:
        parts.append("Anomaly flow is quiet — news mix is statistically normal.")
    top_clusters = ctx.get("top_clusters") or []
    if top_clusters:
        c = top_clusters[0]
        syms = ", ".join((c.get("symbols") or [])[:3])
        reasons = ", ".join((c.get("reasons") or [])[:2])
        coh = c.get("coherence") or 0
        parts.append(
            f"Top event: {syms or '(no symbols)'}"
            + (f" via {reasons}" if reasons else "")
            + f", size {c.get('size') or 0}, coherence {coh:.0%}."
        )
    if shifts > 0:
        parts.append(f"{shifts} regime shift{'s' if shifts != 1 else ''} detected over the window.")
    return " ".join(parts)


def _word_chunks(text: str, group_size: int = 2) -> list[str]:
    """Split a string into word-ish chunks for "typing effect" streaming.

    Used by /api/quant/live-read-stream when DeepSeek is unavailable so the
    deterministic local narrative still arrives chunk-by-chunk over SSE.
    `group_size=2` keeps each frame small enough to feel like a real typing
    animation without flooding the SSE wire with one event per character.
    """
    if not text:
        return []
    parts = text.split()
    out: list[str] = []
    i = 0
    while i < len(parts):
        out.append(" ".join(parts[i : i + group_size]) + " ")
        i += group_size
    if out:
        out[-1] = out[-1].rstrip()
    return out


def _build_explain_prompt(kind: str, payload: dict[str, Any], local: str) -> str:
    """Construct the user-side prompt for DeepSeek narrative."""
    import json as _json

    return (
        f"Quant signal kind: {kind}\n"
        f"Payload (JSON): {_json.dumps(payload, default=str)[:1500]}\n"
        f"Local interpretation: {local}\n\n"
        "Write a 1-3 sentence analyst-grade interpretation. Reference specific "
        "tickers, numbers, or time windows from the payload. Avoid filler."
    )


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
_MARKET_DATA = LocalFixtureMarketDataProvider()


# ── Rate-limit dependency wrappers ──────────────────────────────────────────
# FastAPI's `Depends` reads the dependency function's signature to choose
# which arguments to inject — it can't bind extra positional args to a single
# generic `check_rate(request, bucket, cost)`. The four shims below each pin
# one bucket/cost combination so the route decorator stays short.
#
# Each shim accepts only `request: Request` and forwards into `check_rate`.
# Raising 429 inside the shim short-circuits the route before the handler
# body runs, which is what we want: heavy handlers (DB import, DeepSeek)
# never even start when the bucket is empty.

def _rate_limit_search(request: Request) -> None:
    check_rate(request, SEARCH_BUCKET)


def _rate_limit_db_import(request: Request) -> None:
    # cost=5 burns 5 tokens — combined with the 6-token bucket and
    # 6/min refill, this caps imports at roughly one per minute.
    check_rate(request, DB_IMPORT_BUCKET, cost=5)


def _rate_limit_db_export(request: Request) -> None:
    # cost=3: a full export reads the whole SQLite file off disk;
    # this scales the bucket usage so a `for` loop of exports gets
    # caught even though the per-call API quota is generous.
    check_rate(request, DEFAULT_BUCKET, cost=3)


def _rate_limit_live_read(request: Request) -> None:
    # cost=2: DeepSeek calls are token-priced; this halves the
    # effective per-minute budget vs. a default endpoint.
    check_rate(request, DEFAULT_BUCKET, cost=2)


def _rate_limit_probe(request: Request) -> None:
    # Per-feed probe is a single outbound HTTP fetch — cheap, but humans
    # mashing the button (or a UI loop) shouldn't blow through dozens
    # per minute against a publisher. DB_IMPORT_BUCKET at cost=1 gives
    # ~6 burst + steady 6/min refill, which is comfortable for normal
    # operator use and prompt for runaway clients.
    check_rate(request, DB_IMPORT_BUCKET, cost=1)


_SUPERVISOR: Supervisor | None = None
_SETTINGS: Settings | None = None
_NEWS_POLLER: NewsPoller | None = None
_ARCHIVER: DriveArchiver | None = None
_QUANT_ENGINE = None  # lazy: needs the supervisor's storage; built on first request


def _get_supervisor() -> Supervisor:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        raise HTTPException(status_code=503, detail="supervisor_not_initialized")
    return _SUPERVISOR


def _get_quant_engine():
    """Lazy-build the QuantEngine the first time /api/quant/* is hit.

    Constructing it eagerly in `create_app` would tie quant signal life-
    cycle to sidecar boot — too coarse. Lazy build keeps `import catchem`
    cheap and lets a future settings flip toggle the engine without a
    restart.
    """
    global _QUANT_ENGINE
    if _QUANT_ENGINE is None:
        from .quant import QuantEngine

        sup = _get_supervisor()
        _QUANT_ENGINE = QuantEngine(storage=sup.storage, market_provider=_MARKET_DATA)
    return _QUANT_ENGINE


def _build_news_poller(supervisor: Supervisor, settings: Settings) -> NewsPoller | None:
    """Construct the poller from settings, or None if disabled."""
    if not settings.news.poller_enabled:
        return None
    # Base set = curated DEFAULT_FEEDS + every auto-discovered source pack
    # (GDELT, Reddit, expanded Google News / global wires, …) registered under
    # catchem.news_sources. Operator-configured feeds EXTEND this set rather
    # than replace it, so adding a custom feed never silently drops the
    # curated awareness surface.
    feeds: tuple[FeedSpec, ...] = assemble_feeds()
    if settings.news.feeds:
        extra = tuple(
            FeedSpec(
                name=str(f.get("name", "user")),
                url=str(f["url"]),
                fallback_domain=str(f.get("fallback_domain", "")),
                parser=str(f.get("parser", "rss")),
            )
            for f in settings.news.feeds
            if f.get("url")
        )
        feeds = feeds + extra
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
    # OpenAPI docs are mounted under /api/* so the SPA root ("/") and its
    # history-mode catch-all (`/{full_path:path}`) never shadow them.
    # FastAPI's defaults (/docs, /redoc, /openapi.json) would collide with
    # the SPA fallback's reserved-prefix logic and the catch-all route order.
    app = FastAPI(
        title="catchem",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    cors = (settings or Settings()).api.cors_origins
    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    # ── Per-request path counter ──────────────────────────────────────────
    # Updates `_REQUEST_COUNTS` once per HTTP request. The key is the
    # matched route template (e.g. "/api/quant/dashboard") when FastAPI
    # could resolve one, otherwise the raw URL path. The latter is the
    # common case for 404s and the SPA history fallback — bucketing them
    # under the literal path keeps the wire honest about what the UI was
    # asking for.
    #
    # The hot-path cost is one dict lookup + one assignment per request.
    # No lock — Python dict mutation of single keys is atomic enough for
    # an advisory counter, and a missed +1 under contention is fine
    # (this is a UX gauge, not billing).
    @app.middleware("http")
    async def _request_counter(request: Request, call_next):
        path: str
        try:
            route = request.scope.get("route") if request.scope else None
            tpl = getattr(route, "path", None) if route is not None else None
            path = tpl if isinstance(tpl, str) and tpl else request.url.path
        except Exception:
            path = request.url.path
        _REQUEST_COUNTS[path] = _REQUEST_COUNTS.get(path, 0) + 1
        return await call_next(request)

    # ── Conservative security headers ─────────────────────────────────────
    # Applied to every response. The SPA HTML routes (/, /replay, history
    # fallback) each set their OWN Content-Security-Policy with a per-request
    # nonce — the `setdefault()` below preserves that nonce header verbatim
    # for those responses and only stamps the strict default on everything
    # else (JSON API, healthz, legacy dashboard, error responses incl. 405
    # on HEAD probes).
    #
    # Why two policies? The React SPA needs ONE inline <script> (the FOUC
    # theme bootstrap in `static/app/index.html`); a nonce-based policy
    # permits it without `'unsafe-inline'`. Non-HTML responses don't load
    # any script at all, so they get the strictest policy with no nonce
    # AND no `'unsafe-inline'` on script-src — the default below must
    # never advertise `'unsafe-inline'` for scripts, otherwise a HEAD-
    # probe / 405 / 404 against an HTML route would leak a permissive
    # policy that contradicts the live GET response and confuses smoke
    # tests like `curl -sI /`.
    #
    # Style-src keeps `'unsafe-inline'` everywhere because React inlines
    # style attributes on elements (theme tokens on the root, dynamic chart
    # palettes). A style-nonce policy would also need React-level support
    # which doesn't ship today.
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
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
        html, nonce = _render_spa_with_nonce()
        if html is not None:
            return HTMLResponse(
                html,
                headers={"Content-Security-Policy": _csp_with_nonce(nonce)},
            )
        # Friendly fallback when the bundle hasn't been built yet. The
        # placeholder has no inline <script>, so the middleware's strict
        # default CSP (no nonce) is sufficient here.
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

    @app.get(
        "/api/health/deep",
        summary="Deep liveness/readiness probe across all critical subsystems",
    )
    def api_health_deep() -> Any:
        """Deep health check — verifies all critical subsystems are operational.

        Returns ``200`` with ``{ok: True, ...details}`` if healthy, ``503`` with
        the same envelope (``ok: False``, populated ``issues[]``) if any check
        failed. The 5 checked subsystems are:

          1. ``uptime_seconds`` — sidecar process clock is sane
          2. ``sqlite_ok``     — DB connection + ``SELECT 1`` returns
          3. ``news_poller``   — disabled is fine; if enabled, last_run must
             be within ``5 × interval_seconds`` to be considered fresh
          4. ``schema_ok``     — DB ``user_version`` matches the bundled
             max_known migration version
          5. ``disk_ok``       — at least 100 MB free on the SQLite parent
             directory (informational; only flips to ``False`` below the floor)

        Suitable for K8s liveness/readiness probes:
          * liveness: just ``/healthz`` (returns 200 if process alive)
          * readiness: ``/api/health/deep`` (returns 200 only if subsystems
            ready). The light healthz endpoint is intentionally kept simple
            so a fault here can never wedge the liveness probe.
        """
        issues: list[str] = []
        checks: dict[str, Any] = {}

        # 1. Sidecar uptime check ────────────────────────────────────────
        try:
            uptime = (datetime.now(UTC) - _PROCESS_STARTED_AT).total_seconds()
            checks["uptime_seconds"] = uptime
            checks["uptime_ok"] = uptime > 0
            if uptime <= 0:
                issues.append("uptime_check: non-positive uptime")
        except Exception as e:
            checks["uptime_ok"] = False
            issues.append(f"uptime_check: {e}")

        # Supervisor may be None during early boot — surface that as a
        # subsystem-level failure rather than a 500.
        sup = _SUPERVISOR
        if sup is None:
            checks["supervisor_ok"] = False
            issues.append("supervisor_not_initialized")
        else:
            checks["supervisor_ok"] = True

        # 2. SQLite connectivity ─────────────────────────────────────────
        if sup is not None:
            try:
                with sup.storage._lock, sup.storage._connection() as conn:  # noqa: SLF001
                    cursor = conn.execute("SELECT 1")
                    row = cursor.fetchone()
                    if row is None or int(row[0]) != 1:
                        raise RuntimeError("SELECT 1 returned unexpected value")
                checks["sqlite_ok"] = True
            except Exception as e:
                checks["sqlite_ok"] = False
                issues.append(f"sqlite: {e}")
        else:
            checks["sqlite_ok"] = False

        # 3. News poller status (if enabled) ─────────────────────────────
        try:
            poller = _NEWS_POLLER
            if poller is not None:
                checks["news_poller_enabled"] = True
                last_run = poller.last_run_at
                interval = float(poller.interval_seconds)
                checks["news_poller_interval_seconds"] = interval
                if last_run is not None:
                    elapsed = (datetime.now(UTC) - last_run).total_seconds()
                    stale_threshold = 5.0 * interval
                    checks["news_poller_last_run_seconds_ago"] = elapsed
                    if elapsed > stale_threshold:
                        issues.append(
                            f"news_poller_stale: {elapsed:.0f}s since last poll "
                            f"(threshold {stale_threshold:.0f}s)"
                        )
                        checks["news_poller_ok"] = False
                    else:
                        checks["news_poller_ok"] = True
                else:
                    # Just started, no run yet — treat as healthy (process
                    # has the startup grace window before the first tick).
                    checks["news_poller_ok"] = True
                    checks["news_poller_last_run_seconds_ago"] = None
            else:
                checks["news_poller_enabled"] = False
                checks["news_poller_ok"] = True  # disabled is fine
        except Exception as e:
            checks["news_poller_ok"] = False
            issues.append(f"news_poller: {e}")

        # 4. Schema version check ────────────────────────────────────────
        if sup is not None:
            try:
                from .migrations import current_version, max_known_version

                with sup.storage._lock, sup.storage._connection() as conn:  # noqa: SLF001
                    current = current_version(conn)
                max_known = max_known_version()
                checks["schema_version"] = current
                checks["schema_max_known"] = max_known
                if current < max_known:
                    issues.append(f"schema_outdated: {current} < {max_known}")
                    checks["schema_ok"] = False
                else:
                    checks["schema_ok"] = True
            except Exception as e:
                checks["schema_ok"] = False
                issues.append(f"schema: {e}")
        else:
            checks["schema_ok"] = False

        # 5. Disk space check ────────────────────────────────────────────
        # Informational by default — only flips below the 100 MB floor to
        # keep readiness aligned with the operator's typical local-first
        # situation (a dev machine often has TBs free; a CI runner has
        # gigs; both are fine).
        try:
            s = _SETTINGS or load_settings()
            db_path = s.sqlite_path()
            import shutil

            usage = shutil.disk_usage(db_path.parent)
            free_mb = usage.free / 1024 / 1024
            checks["disk_free_mb"] = free_mb
            if usage.free < 100 * 1024 * 1024:  # < 100 MB
                issues.append(f"disk_low: {free_mb:.0f}MB free")
                checks["disk_ok"] = False
            else:
                checks["disk_ok"] = True
        except Exception as e:
            checks["disk_ok"] = False
            issues.append(f"disk: {e}")

        ok = len(issues) == 0
        response: dict[str, Any] = {
            "ok": ok,
            "checks": checks,
            "issues": issues,
            "generated_at": datetime.now(UTC).isoformat(),
            "schema_version": 1,
        }

        if not ok:
            # 503 so K8s readiness probe can mark the pod not-ready
            # without killing it (liveness stays on /healthz).
            return Response(
                content=json.dumps(response),
                status_code=503,
                media_type="application/json",
            )
        return response

    @app.get("/api/_index", summary="List every API/UI/health route")
    def api_index() -> dict[str, Any]:
        """Programmatic listing of routes for the help/debug surface.

        Returns a sorted list of `{path, method, summary}` entries, where
        `summary` falls back to the first line of the handler docstring when
        no explicit summary was set. Only paths under `/api`, `/ui`, and
        `/healthz` are included — the SPA catch-all and asset mounts are
        intentionally hidden.
        """
        out: list[dict[str, str]] = []
        for route in app.routes:
            path = getattr(route, "path", None)
            if not isinstance(path, str):
                continue
            if not path.startswith(("/api", "/ui", "/healthz")):
                continue
            raw_methods = getattr(route, "methods", None) or set()
            methods = sorted(set(raw_methods) - {"HEAD", "OPTIONS"})
            if not methods:
                continue
            summary = getattr(route, "summary", "") or ""
            if not summary:
                endpoint = getattr(route, "endpoint", None)
                doc = getattr(endpoint, "__doc__", "") or ""
                summary = doc.strip().split("\n", 1)[0].strip()
            for m in methods:
                out.append({"path": path, "method": m, "summary": summary})
        out.sort(key=lambda p: (p["path"], p["method"]))
        return {"paths": out, "total": len(out), "schema_version": 1}

    @app.get("/config")
    def config() -> dict[str, Any]:
        s = _SETTINGS or load_settings()
        return {
            "mode": s.mode.value,
            "use_ml_stubs": s.models.use_ml_stubs,
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
        status.setdefault("generated_at", datetime.now(UTC).isoformat())
        return status

    @app.get("/api/stats", summary="Runtime telemetry: uptime, request counts, DB stats")
    def api_stats() -> dict[str, Any]:
        """Operational telemetry for the Ops cockpit.

        Surfaces uptime, the per-path request counter populated by the
        `_request_counter` middleware, raw DB row counts (records / reviews
        / dlq) read directly from SQLite under the storage lock, and the
        DeepSeek reviewer's cumulative USD spend if the registry is
        active. Sensitive config (API keys, webhook URLs) is intentionally
        omitted — this surface is safe to mirror into a log file.

        The payload is cached for ~2s (`_STATS_TTL_SECONDS`) so a UI
        polling at 5s never stampedes the storage lock when multiple
        Ops windows are open.
        """
        now_ts = time.monotonic()
        with _STATS_CACHE_LOCK:
            cached = _STATS_CACHE.get("payload")
            expires_at = float(_STATS_CACHE.get("expires_at") or 0.0)
            if cached is not None and now_ts < expires_at:
                return cached

        sup = _get_supervisor()
        uptime_seconds = (_dt.now(UTC) - _SUPERVISOR_START_TIME).total_seconds()

        # DB row counts — go through the storage lock so a concurrent
        # writer doesn't race the read. `reviews` and `dlq` are guaranteed
        # by the schema bootstrap; no `IF EXISTS` check needed.
        storage = sup.storage
        with storage._lock, storage._connection() as conn:  # noqa: SLF001 — intentional shared lock
            records_count = int(
                conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            )
            reviews_count = int(
                conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            )
            dlq_count = int(
                conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
            )

        # DeepSeek cumulative spend — only meaningful when the registry
        # exposes the cached budget state. Falls back to 0.0 cleanly when
        # the reviewer is disabled / unkeyed.
        deepseek_spent = 0.0
        try:
            registry = getattr(sup, "reviewers", None)
            if registry is not None and hasattr(registry, "budget_state"):
                deepseek_spent = float(registry.budget_state().spent_usd)
        except Exception:
            deepseek_spent = 0.0

        # Snapshot the counter dict so the response payload is not aliased
        # to the live map — otherwise a subsequent +1 would mutate the
        # cached payload and serialize differently on the next call.
        counts_snapshot = dict(_REQUEST_COUNTS)

        s = _SETTINGS or load_settings()
        # `Settings` does not (yet) expose `app_version`; surface the
        # FastAPI app's version tag when callable, otherwise None. Kept
        # optional in the response contract.
        app_version = getattr(s, "app_version", None) or getattr(app, "version", None)

        # Process-level telemetry — RSS, CPU%, thread count. The helper
        # returns a zero-filled snapshot with ``available=False`` on builds
        # that ship without psutil, so the wire shape is stable either way
        # and the UI can render an "(estimate)" badge based on the flag.
        # The 50ms cpu_percent sample fires only on the psutil branch; with
        # the 2s response cache it's well inside the <100ms budget.
        proc_metrics = _current_process_metrics()

        payload: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": _dt.now(UTC).isoformat(),
            "uptime_seconds": uptime_seconds,
            "request_counts": counts_snapshot,
            "total_requests": sum(counts_snapshot.values()),
            "db": {
                "records": records_count,
                "reviews": reviews_count,
                "dlq": dlq_count,
            },
            "reviewers": {
                "deepseek_usd_spent": deepseek_spent,
                "stub_active": True,
            },
            "process": {
                "rss_mb": proc_metrics.rss_mb,
                "vms_mb": proc_metrics.vms_mb,
                "cpu_percent": proc_metrics.cpu_percent,
                "num_threads": proc_metrics.num_threads,
                "psutil_available": proc_metrics.available,
            },
            "version": app_version,
        }
        with _STATS_CACHE_LOCK:
            # Two writes under one lock so a concurrent reader never sees
            # the new payload with the stale expires_at (or vice versa).
            _STATS_CACHE["payload"] = payload
            _STATS_CACHE["expires_at"] = now_ts + _STATS_TTL_SECONDS
        return payload

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

    # ── User-defined record tags ─────────────────────────────────────────
    # Free-form analyst tags layered on top of the pipeline-derived
    # asset_class / reason_code / symbol labels. Kept in a side table
    # (``record_tags``, migration v2) so a re-ingest of the same record
    # doesn't blow them away. Validation lives in
    # :func:`catchem.storage._validate_tag`; the regex below is duplicated
    # for HTTP-422 framing — storage still rejects invalid input even if
    # the API layer ever gets bypassed.
    _TAG_API_PATTERN = re.compile(r"^[a-zA-Z0-9_\-.]+$")

    @app.get("/api/records/{capture_id}/tags")
    def api_get_tags(capture_id: str) -> dict[str, Any]:
        sup = _get_supervisor()
        return {
            "capture_id": capture_id,
            "tags": sup.storage.get_record_tags(capture_id),
        }

    @app.post("/api/records/{capture_id}/tags")
    def api_add_tag(capture_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw = payload.get("tag")
        tag = (raw or "").strip() if isinstance(raw, str) else ""
        if not tag or len(tag) > 50 or not _TAG_API_PATTERN.match(tag):
            raise HTTPException(status_code=400, detail="invalid tag")
        sup = _get_supervisor()
        # Confirm the capture exists so we don't silently insert tags
        # against a deleted/unknown record (FK is enforced at the storage
        # level, but sqlite FK enforcement is off by default for this
        # connection; an explicit lookup gives the API a clean 404).
        if sup.storage.get_record(capture_id) is None:
            raise HTTPException(status_code=404, detail="capture_not_found")
        try:
            added = sup.storage.add_record_tag(capture_id, tag)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "added": added,
            "tags": sup.storage.get_record_tags(capture_id),
        }

    @app.delete("/api/records/{capture_id}/tags/{tag}")
    def api_remove_tag(capture_id: str, tag: str) -> dict[str, Any]:
        sup = _get_supervisor()
        try:
            removed = sup.storage.remove_record_tag(capture_id, tag)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "removed": removed,
            "tags": sup.storage.get_record_tags(capture_id),
        }

    @app.get("/api/tags")
    def api_list_tags(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        sup = _get_supervisor()
        return {"items": sup.storage.top_tags(limit)}

    @app.get("/api/tags/{tag}/records", response_model=RecordListResponse)
    def api_records_by_tag(
        tag: str, limit: int = Query(50, ge=1, le=500)
    ) -> RecordListResponse:
        sup = _get_supervisor()
        try:
            records = sup.storage.records_by_tag(tag, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RecordListResponse(items=_to_summary_list(records, _is_production_safe()))

    @app.post("/replay")
    def replay(
        # Bounded both ends: ge=1 keeps `max_records=0` from sneaking
        # through to the supervisor (the awareness_replay short-circuit is
        # `if max_records and ...`, so 0 is treated as "no cap" and
        # iterates every file — a 0 from the wire would block the loop
        # for minutes on a populated awareness dir). le=1_000_000 caps
        # the realistic upper bound; an operator typo of 99_999_999 still
        # gets a clean 422.
        max_records: int = Body(50, embed=True, ge=1, le=1_000_000),
    ) -> dict[str, Any]:
        sup = _get_supervisor()
        return sup.run_replay(max_records=max_records)

    @app.post("/process-one", response_model=FinancialImpactDetail)
    def process_one(capture: AwarenessCaptureView = Body(...)) -> FinancialImpactDetail:
        # Typing the body as the schema (instead of `dict`) lets FastAPI
        # surface pydantic ValidationErrors as a clean 422 with field-level
        # details. Pre-fix the route accepted `dict` and called
        # `model_validate` manually — the resulting ValidationError escaped
        # as a 500 Internal Server Error with a noisy traceback in the
        # sidecar log instead of an actionable response for the caller.
        sup = _get_supervisor()
        rec = sup.process_capture(capture)
        payload = rec.model_dump(mode="json")
        redacted = redact_record_for_mode(payload, production_safe=_is_production_safe()) or payload
        return FinancialImpactDetail(**_normalize_detail_payload(redacted))

    # ────────────────────────────────────────────────────────────────────────
    # Catchem desktop endpoints — typed wrappers around demo.py + safe upload.
    # All preserve the production-safe redaction guarantees.
    # ────────────────────────────────────────────────────────────────────────

    def _demo_to_response(result: _DemoResult) -> DemoRunResponse:
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
                "created_at": _dt.now(UTC).isoformat(),
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        effective_title = (title or title_hint or "(untitled upload)").strip()
        result = _run_demo(title=effective_title, text=body_text, domain=domain or "demo.local", url=url)
        return _demo_to_response(result)

    # ────────────────────────────────────────────────────────────────────────
    # Second-opinion reviewer endpoints (DeepSeek etc.). All under /api/
    # to mirror the modern surface; the legacy /ui/ prefix is reserved for
    # storage-truth surfaces only.
    # ────────────────────────────────────────────────────────────────────────

    @app.get("/api/reviews/status")
    def reviews_status() -> dict[str, Any]:
        """Live reviewer registry status — wired by the Settings panel."""
        sup = _get_supervisor()
        s = _SETTINGS or load_settings()
        return {
            **sup.reviewers.status(),
            "primary_reviewer_version": sup.reviewers.stub().reviewer_version,
            "tokens": sup.storage.review_token_totals("deepseek"),
            "base_url": s.reviewers.deepseek.base_url,
            "generated_at": _dt.now(UTC).isoformat(),
        }

    @app.get("/api/reviews/spend-history")
    def api_spend_history(days: int = Query(7, ge=1, le=30)) -> dict[str, Any]:
        """Aggregate DeepSeek review costs per day for the last N days.

        Powers the spend sparkline in the Settings → DeepSeek reviewer card.
        Uses parameterized SQL (no string concatenation), DATE() bucketing on
        created_at, and the same shared storage lock as the rest of the API
        so the read doesn't race the writer.
        """
        sup = _get_supervisor()
        storage = sup.storage
        cutoff = (_dt.now(UTC) - timedelta(days=days)).isoformat()
        with storage._lock, storage._connection() as conn:  # noqa: SLF001 — intentional shared lock
            rows = conn.execute(
                """
                SELECT
                    DATE(created_at) AS day,
                    COUNT(*) AS call_count,
                    COALESCE(SUM(usd_cost), 0.0) AS total_cost
                FROM reviews
                WHERE reviewer_id = ? AND created_at >= ?
                GROUP BY DATE(created_at)
                ORDER BY day DESC
                """,
                ("deepseek", cutoff),
            ).fetchall()
        history = [
            {
                "day": r["day"],
                "call_count": int(r["call_count"]),
                "total_cost_usd": float(r["total_cost"]),
            }
            for r in rows
        ]
        return {
            "schema_version": 1,
            "generated_at": _dt.now(UTC).isoformat(),
            "days": days,
            "history": history,
            "totals": {
                "calls": sum(h["call_count"] for h in history),
                "cost_usd": sum(h["total_cost_usd"] for h in history),
            },
        }

    @app.post("/api/reviews/{capture_id}/run")
    def reviews_run_on_demand(capture_id: str) -> dict[str, Any]:
        """Manually trigger a DeepSeek review for an existing capture.

        The compare page exposes this as a per-row button so the analyst
        can pull a second opinion on a record that wasn't naturally
        sampled. Returns 404 if the capture is not in storage, 503 if
        DeepSeek is disabled.
        """
        sup = _get_supervisor()
        rec = sup.storage.get_record(capture_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"unknown capture_id: {capture_id}")
        # Reconstruct a minimal AwarenessCaptureView from the stored
        # record so we don't need to keep the raw upstream JSONL row.
        cap = AwarenessCaptureView(
            capture_id=rec["capture_id"],
            doc_id=rec["doc_id"],
            title=rec.get("title"),
            text=rec.get("text_excerpt") or "",
            language=rec.get("language"),
            url=rec.get("url"),
            domain=rec.get("domain"),
            published_ts=rec.get("published_ts"),
        )
        # Ensure the stub row exists for compare-view symmetry.
        try:
            from .reviewers import record_to_review_payload
            from .schemas import FinancialImpactRecord as _Rec

            primary = _Rec(**rec)
            stub_payload = record_to_review_payload(
                primary,
                reviewer_id=sup.reviewers.stub().reviewer_id,
                reviewer_version=sup.reviewers.stub().reviewer_version,
            )
            sup.storage.upsert_review(stub_payload.to_storage_row())
        except Exception:
            # Non-fatal — DeepSeek call still runs; compare view will
            # render only the DeepSeek side.
            pass
        client = sup.reviewers.deepseek()
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="DeepSeek reviewer disabled or missing API key (see Settings)",
            )
        if sup.reviewers.budget_state().exhausted:
            raise HTTPException(status_code=402, detail="DeepSeek USD cap exceeded")
        payload = sup.reviewers.run_and_persist_deepseek(cap)
        return {
            "ok": payload is not None and payload.error_code is None,
            "capture_id": capture_id,
            "review": payload.to_storage_row() if payload else None,
        }

    @app.get("/api/reviews/compare")
    def reviews_compare(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        """Pair (stub, deepseek) review rows for the compare dashboard."""
        sup = _get_supervisor()
        pairs = sup.storage.reviews_with_pair("stub", "deepseek", limit=limit)
        items: list[dict[str, Any]] = []
        for stub_row, ds_row in pairs:
            cap_id = stub_row["capture_id"]
            rec = sup.storage.get_record(cap_id) or {}
            items.append(
                {
                    "capture_id": cap_id,
                    "title": rec.get("title"),
                    "domain": rec.get("domain"),
                    "url": rec.get("url"),
                    "stub": stub_row,
                    "deepseek": ds_row,
                    "agreement": _compute_agreement(stub_row["payload"], ds_row["payload"]),
                }
            )
        return {
            "items": items,
            "summary": _compute_compare_summary(items),
            "generated_at": _dt.now(UTC).isoformat(),
        }

    @app.patch("/api/reviews/settings")
    def reviews_patch_settings(patch: dict = Body(...)) -> dict[str, Any]:
        """Runtime-toggle the reviewer surface without restarting the sidecar.

        Accepts a subset of:
          enabled, sampling_rate, usd_cap, api_key, model, base_url
        Returns the new status dict.
        """
        sup = _get_supervisor()
        cfg = sup.settings.reviewers.deepseek
        if "enabled" in patch:
            cfg.enabled = bool(patch["enabled"])
        if "sampling_rate" in patch:
            rate = float(patch["sampling_rate"])
            cfg.sampling_rate = max(0.0, min(1.0, rate))
        if "usd_cap" in patch:
            cfg.usd_cap = max(0.0, float(patch["usd_cap"]))
        if "api_key" in patch:
            cfg.api_key = str(patch["api_key"] or "")
        if "model" in patch:
            cfg.model = str(patch["model"]).strip() or cfg.model
        if "base_url" in patch:
            cfg.base_url = str(patch["base_url"]).strip() or cfg.base_url
        # Reset the cached client so the next call picks up the new key/model.
        sup.reviewers._deepseek = None  # noqa: SLF001 — registry-internal cache
        sup.reviewers.invalidate_budget_cache()
        return sup.reviewers.status()

    # ────────────────────────────────────────────────────────────────────────
    # Backtest — calibrate stub-vs-DeepSeek over the last N paired reviews.
    # Ground-truth proxy: DeepSeek's `finance_relevance_score`. Lives under
    # /api/backtest so the UI can poll it like any other quant signal. No
    # external market data — we re-use the reviewer ledger we already keep.
    # ────────────────────────────────────────────────────────────────────────

    @app.get("/api/backtest")
    def api_backtest(sample_size: int = Query(200, ge=10, le=2000)) -> dict[str, Any]:
        """Quant backtest over the last `sample_size` paired (stub, deepseek) rows.

        Returns a stable envelope:
          schema_version            → bump when the shape changes
          ran_at                    → wall-clock at evaluation time
          summary                   → headline metrics (always populated)
          calibration_bins          → per-quintile predicted vs ground-truth
          predictions_sample        → up to 50 raw (predicted, gt, delta) rows

        Out-of-band errors do NOT raise — the BacktestRun zero-state lets
        the UI render an honest "no paired reviews yet" empty state.
        """
        sup = _get_supervisor()
        from .backtest import run_backtest

        result = run_backtest(sup, sample_size=sample_size)
        return {
            "schema_version": result.schema_version,
            "ran_at": _dt.now(UTC).isoformat(),
            "sample_size": sample_size,
            "summary": result.summary,
            "calibration_bins": result.calibration_bins,
            "predictions_sample": result.relevance_predictions,
        }

    # ────────────────────────────────────────────────────────────────────────
    # Webhook output (Slack / Discord / Teams) — config + test endpoints.
    # The URL is a soft secret (Slack encodes an auth token in the path) so
    # it's redacted in GET payloads — the UI gets back `url_configured: bool`,
    # never the raw URL.
    # ────────────────────────────────────────────────────────────────────────

    def _webhook_status_view() -> dict[str, Any]:
        s = _SETTINGS or load_settings()
        cfg = s.webhook
        sup = _SUPERVISOR  # may be None during early lifespan
        stats = sup.webhook_stats if sup is not None else {
            "attempted": 0, "sent": 0, "filtered": 0, "failed": 0,
        }
        return {
            "enabled": cfg.enabled,
            "url_configured": bool(cfg.url),
            "min_score": cfg.min_score,
            "asset_class_filter": cfg.asset_class_filter,
            "reason_code_filter": cfg.reason_code_filter,
            "timeout_seconds": cfg.timeout_seconds,
            "stats": dict(stats),
            "last_status": getattr(sup, "webhook_last_status", None) if sup else None,
            "last_error": getattr(sup, "webhook_last_error", None) if sup else None,
            "generated_at": _dt.now(UTC).isoformat(),
        }

    @app.get("/api/webhook/config")
    def webhook_get_config() -> dict[str, Any]:
        """Current webhook configuration. URL is redacted to a boolean.

        Returns `url_configured: bool` instead of the raw URL so the
        Settings panel can render an "API key configured ✓" style chip
        without ever shipping the secret back to the SPA. The frontend
        only needs to know whether the URL is set; users replace it via
        POST `/api/webhook/config`.
        """
        return _webhook_status_view()

    @app.post("/api/webhook/config")
    def webhook_post_config(patch: dict = Body(...)) -> dict[str, Any]:
        """Update webhook configuration. Returns the redacted status view.

        Accepts a subset of:
          enabled, url, min_score, asset_class_filter, reason_code_filter,
          timeout_seconds

        `url`: must start with http:// or https://. Empty string clears it.
        `min_score`: clamped to [0.0, 1.0].
        Filters: list[str] or null (null clears the filter).
        """
        from .webhook import is_valid_webhook_url

        s = _SETTINGS or load_settings()
        cfg = s.webhook
        if "enabled" in patch:
            cfg.enabled = bool(patch["enabled"])
        if "url" in patch:
            new_url = str(patch["url"] or "").strip()
            if new_url and not is_valid_webhook_url(new_url):
                raise HTTPException(
                    status_code=422,
                    detail="webhook url must start with http:// or https:// and include a host",
                )
            cfg.url = new_url
        if "min_score" in patch:
            try:
                score = float(patch["min_score"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="min_score must be a number") from exc
            cfg.min_score = max(0.0, min(1.0, score))
        if "asset_class_filter" in patch:
            val = patch["asset_class_filter"]
            if val is None:
                cfg.asset_class_filter = None
            elif isinstance(val, list):
                cfg.asset_class_filter = [str(v) for v in val if v]
            else:
                raise HTTPException(status_code=422, detail="asset_class_filter must be a list or null")
        if "reason_code_filter" in patch:
            val = patch["reason_code_filter"]
            if val is None:
                cfg.reason_code_filter = None
            elif isinstance(val, list):
                cfg.reason_code_filter = [str(v) for v in val if v]
            else:
                raise HTTPException(status_code=422, detail="reason_code_filter must be a list or null")
        if "timeout_seconds" in patch:
            try:
                t = float(patch["timeout_seconds"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="timeout_seconds must be a number") from exc
            cfg.timeout_seconds = max(0.5, min(60.0, t))
        return _webhook_status_view()

    @app.post(
        "/api/webhook/test",
        dependencies=[Depends(_rate_limit_db_import)],
    )
    def webhook_test(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
        """Send a test webhook with a synthetic record.

        Uses the cost=5 import bucket so a user clicking "Test" ten
        times in a row hits the limiter long before they accidentally
        spam their channel. The synthetic record always carries
        score=0.99 so the filters never reject it — the test verifies
        delivery, not the gate logic.

        Returns a 200 envelope even when the URL is missing:
          {"ok": false, "status": "no_url_configured", "url_configured": false, ...}
        The UI renders the friendly string instead of having to parse a 422.
        """
        from .webhook import send_webhook

        s = _SETTINGS or load_settings()
        cfg = s.webhook
        if not cfg.url:
            # Friendly 200 envelope so the SPA branch can render an
            # actionable hint without juggling HTTP status codes. The
            # button is disabled in this state anyway, but the endpoint
            # remains safely callable (e.g. via curl) and tells the
            # caller exactly why nothing fired.
            return {
                "ok": False,
                "status": "no_url_configured",
                "url_configured": False,
                "generated_at": _dt.now(UTC).isoformat(),
            }
        sample = {
            "capture_id": "test-webhook-" + _dt.now(UTC).strftime("%Y%m%dT%H%M%S"),
            "title": str(body.get("title") or "Catchem webhook test"),
            "url": str(body.get("url") or "https://catchem.local/test"),
            "domain": str(body.get("domain") or "catchem.local"),
            "finance_relevance_score": 0.99,
            "asset_classes": list(body.get("asset_classes") or ["equities"]),
            "impact_reason_codes": list(body.get("impact_reason_codes") or ["earnings"]),
            "candidate_symbols": list(body.get("candidate_symbols") or ["AAPL"]),
        }
        # Force-bypass the enabled/url + filter gates for the test path:
        # we want the operator to be able to verify their URL even before
        # they flip the master switch, and even when the configured
        # min_score / asset / reason filters would normally exclude the
        # synthetic payload.
        from .settings import WebhookConfig

        test_cfg = WebhookConfig(
            enabled=True,
            url=cfg.url,
            min_score=0.0,
            asset_class_filter=None,
            reason_code_filter=None,
            timeout_seconds=cfg.timeout_seconds,
        )
        ok, status = send_webhook(sample, test_cfg)
        return {
            "ok": ok,
            "status": status,
            "url_configured": True,
            "generated_at": _dt.now(UTC).isoformat(),
        }

    # ────────────────────────────────────────────────────────────────────────
    # Awareness Quant Lens — depth analytics on top of the primary records.
    # All endpoints are read-only and cache-backed; safe to poll from the UI.
    # ────────────────────────────────────────────────────────────────────────

    @app.get("/api/quant/dashboard")
    def quant_dashboard(limit: int = Query(500, ge=10, le=5000)) -> dict[str, Any]:
        """Single-call payload for the /scan cockpit."""
        engine = _get_quant_engine()
        return {
            **engine.dashboard_snapshot(limit=limit),
            "generated_at": _dt.now(UTC).isoformat(),
        }

    @app.get("/api/quant/clusters")
    def quant_clusters(
        limit: int = Query(500, ge=10, le=5000),
        window_seconds: int = Query(1800, ge=60, le=86400),
        similarity_threshold: float = Query(0.35, ge=0.0, le=1.0),
        min_cluster_size: int = Query(2, ge=1, le=20),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        cs = engine.clusters(
            limit=limit,
            window_seconds=window_seconds,
            similarity_threshold=similarity_threshold,
            min_cluster_size=min_cluster_size,
        )
        return {"items": [asdict(c) for c in cs], "total": len(cs)}

    @app.get("/api/quant/sources")
    def quant_sources(
        limit: int = Query(1000, ge=10, le=5000),
        window_days: int = Query(30, ge=1, le=365),
        min_records: int = Query(3, ge=1, le=100),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        lb = engine.source_leaderboard(
            limit=limit, window_days=window_days, min_records=min_records
        )
        return asdict(lb) if lb else {"window_days": window_days, "total_records": 0, "total_domains": 0, "sources": []}

    @app.get("/api/quant/novelty")
    def quant_novelty(limit: int = Query(200, ge=10, le=1000)) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        items = engine.novelty_timeline(limit=limit)
        return {"items": [asdict(n) for n in items], "total": len(items)}

    @app.get("/api/quant/novelty/{capture_id}")
    def quant_novelty_one(capture_id: str) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        result = engine.novelty_for(capture_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown capture_id: {capture_id}")
        return asdict(result)

    @app.get("/api/quant/lead-lag")
    def quant_lead_lag(limit: int = Query(500, ge=10, le=5000)) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.lead_lag(limit=limit)
        if report is None:
            return {"total_events": 0, "total_sources": 0, "per_event": [], "per_source": []}
        return asdict(report)

    @app.get("/api/quant/regime")
    def quant_regime(
        limit: int = Query(1000, ge=10, le=5000),
        bucket_minutes: int = Query(60, ge=5, le=1440),
        shift_threshold: float = Query(0.40, ge=0.0, le=10.0),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.regime(
            limit=limit, bucket_minutes=bucket_minutes, shift_threshold=shift_threshold
        )
        if report is None:
            return {"bucket_minutes": bucket_minutes, "shift_threshold": shift_threshold, "buckets": [], "detected_shifts": []}
        return asdict(report)

    @app.get("/api/quant/reaction/{capture_id}")
    def quant_reaction(capture_id: str) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.reaction_for(capture_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"unknown capture_id or no record: {capture_id}")
        return asdict(report)

    def _live_read_context(limit: int) -> dict[str, Any]:
        """Shared compact-context builder for live-read + live-read-stream.

        Hits the engine snapshot cache, distills it into the tight summary
        DeepSeek sees, and returns the dict ready for both the JSON-mode
        endpoint and the SSE stream. Kept inline-closure-style so it can
        reach `_get_quant_engine` without re-importing the module state.
        """
        engine = _get_quant_engine()
        snap = engine.dashboard_snapshot(limit=limit)
        n_records = snap.get("n_records_window", 0)
        n_clusters = snap.get("n_clusters", 0)
        regime = snap.get("regime") or {}
        anomalies = snap.get("anomalies") or {}
        spillover = snap.get("spillover") or {}
        sent_mom = snap.get("sentiment_momentum") or {}
        sources = (snap.get("source_leaderboard") or {}).get("sources", [])
        top_clusters = snap.get("clusters", [])[:3]
        return {
            "window_records": n_records,
            "clusters_active": n_clusters,
            "regime_shifts_recent": len(regime.get("detected_shifts", [])),
            "volume_anomalies": len(anomalies.get("volume_anomalies", [])),
            "sentiment_shocks": len(anomalies.get("sentiment_shocks", [])),
            "symbol_bursts": [
                {"symbol": b.get("symbol"), "observed": b.get("observed"), "z": b.get("z_score")}
                for b in (anomalies.get("symbol_bursts") or [])[:3]
            ],
            "spillover_edges": [
                {"src": e.get("source_asset"), "dst": e.get("target_asset"), "score": e.get("spillover_score")}
                for e in (spillover.get("edges") or [])[:3]
            ],
            "top_tickers": [
                {"symbol": t.get("symbol"), "momentum": t.get("momentum"), "direction": t.get("direction")}
                for t in (sent_mom.get("tickers") or [])[:5]
            ],
            "top_clusters": [
                {
                    "size": c.get("size"),
                    "coherence": c.get("coherence"),
                    "symbols": c.get("dominant_symbols", [])[:3],
                    "reasons": c.get("dominant_reasons", [])[:2],
                    "domains": c.get("member_domains", [])[:3],
                }
                for c in top_clusters
            ],
            "top_source": (sources[0] if sources else {}).get("domain"),
        }

    # The system prompt is shared by the JSON and streaming endpoints; the
    # streaming path can't change it (the UI would render different text)
    # so it lives at module scope inside the closure.
    _LIVE_READ_SYSTEM_PROMPT = (
        "You are a senior financial-news analyst writing a live read for a trading-desk dashboard. "
        "Given a JSON snapshot of quant signals, write 2-4 SHORT sentences answering: "
        "(1) What is the dominant story right now? (2) Where is risk concentrated? "
        "(3) Anything counterintuitive worth a deeper look? "
        "Reference specific tickers, asset classes, and numbers from the payload. "
        "Avoid filler, hedging language, or generic recommendations."
    )

    @app.get("/api/quant/live-read", dependencies=[Depends(_rate_limit_live_read)])
    def quant_live_read(limit: int = Query(1000, ge=100, le=5000)) -> dict[str, Any]:
        """DeepSeek-narrated "what's happening RIGHT NOW" summary.

        Powers the hero on /scan. Cached at the engine level (30s) so
        the analyst can leave the page open without burning tokens; on
        cache hit returns instantly. Falls back to a deterministic local
        narrative if DeepSeek is disabled.
        """
        sup = _get_supervisor()
        compact = _live_read_context(limit)
        local = _local_live_read(compact)
        client = sup.reviewers.deepseek()
        if client is None or sup.reviewers.budget_state().exhausted:
            return {"narrative": local, "source": "local", "context": compact, "generated_at": _dt.now(UTC).isoformat()}
        try:
            import json as _json
            req = {
                "model": client.model,
                "messages": [
                    {"role": "system", "content": _LIVE_READ_SYSTEM_PROMPT},
                    {"role": "user", "content": _json.dumps(compact)[:1500]},
                ],
                "temperature": 0.35,
                "max_tokens": 320,
            }
            response = client._client.post(  # noqa: SLF001
                f"{client._base_url}/chat/completions",
                json=req,
                headers={"Authorization": f"Bearer {client._api_key}", "Content-Type": "application/json"},  # noqa: SLF001
            )
            if response.status_code != 200:
                return {"narrative": local, "source": "local", "fallback_reason": f"http_{response.status_code}", "context": compact, "generated_at": _dt.now(UTC).isoformat()}
            envelope = response.json()
            content = (envelope.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            usage = envelope.get("usage") or {}
            usd = client.estimate_usd(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
            sup.reviewers.add_spend(usd)
            return {
                "narrative": content or local,
                "source": "deepseek",
                "usd_cost": usd,
                "context": compact,
                "generated_at": _dt.now(UTC).isoformat(),
            }
        except Exception as exc:
            return {"narrative": local, "source": "local", "fallback_reason": str(exc)[:120], "context": compact, "generated_at": _dt.now(UTC).isoformat()}

    @app.get("/api/quant/live-read-stream", dependencies=[Depends(_rate_limit_live_read)])
    async def quant_live_read_stream(limit: int = Query(1000, ge=100, le=5000)) -> EventSourceResponse:
        """SSE stream of the DeepSeek live-read narrative, chunk-by-chunk.

        Same shape as `/api/quant/live-read` but emits `event: chunk` frames
        as DeepSeek produces tokens, then a final `event: done` with the
        total usage / cost / fallback flag. The hero on /scan attaches an
        EventSource and renders the running buffer as a typing effect.

        Frames:
            event: start  → {limit, source, generated_at}
            event: chunk  → {text}     (one or more)
            event: usage  → {usd_cost} (DeepSeek only, after last chunk)
            event: done   → {ok, source, fallback_reason?}
            event: error  → {error}    (terminal — wire-level failure)

        When DeepSeek is unavailable (no key, disabled, budget exhausted,
        or transport failure), the endpoint streams the deterministic
        local narrative word-by-word so the UI animation still plays.
        """
        # Build the snapshot context BEFORE the generator starts so any
        # exception (storage down, etc.) lands as an HTTP error rather
        # than as a half-open SSE wire.
        compact = _live_read_context(limit)
        local = _local_live_read(compact)
        sup = _get_supervisor()
        client = sup.reviewers.deepseek()
        budget_exhausted = sup.reviewers.budget_state().exhausted
        use_deepseek = client is not None and not budget_exhausted

        async def gen() -> AsyncIterator[dict[str, Any]]:
            generated_at = _dt.now(UTC).isoformat()
            yield {
                "event": "start",
                "data": json.dumps(
                    {
                        "limit": limit,
                        "source": "deepseek" if use_deepseek else "local",
                        "generated_at": generated_at,
                    }
                ),
            }

            if not use_deepseek:
                # Stream the deterministic local narrative word-by-word so
                # the UI animation still feels responsive when DeepSeek is
                # unavailable. Tiny await keeps the loop yielding.
                reason = "deepseek_disabled" if client is None else "budget_exhausted"
                for token in _word_chunks(local):
                    yield {"event": "chunk", "data": json.dumps({"text": token})}
                    await asyncio.sleep(0)
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "ok": True,
                            "source": "local",
                            "fallback_reason": reason,
                            "generated_at": generated_at,
                        }
                    ),
                }
                return

            # DeepSeek streaming path. We capture text + usage envelopes
            # from the async iterator helper, fall back to local narrative
            # if no content ever arrives (e.g. immediate 5xx), and surface
            # transport errors as `event: error` followed by `done`.
            from .reviewers.deepseek import stream_chat_completion

            assert client is not None  # mypy hint — guarded above
            messages = [
                {"role": "system", "content": _LIVE_READ_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(compact)[:1500]},
            ]
            collected_text: list[str] = []
            usage_payload: dict[str, Any] | None = None
            error_msg: str | None = None
            try:
                async for envelope in stream_chat_completion(
                    api_key=client._api_key,  # noqa: SLF001
                    base_url=client._base_url,  # noqa: SLF001
                    model=client.model,
                    messages=messages,
                    temperature=0.35,
                    max_tokens=320,
                ):
                    kind = envelope.get("type")
                    if kind == "delta":
                        text = envelope.get("text") or ""
                        if text:
                            collected_text.append(text)
                            yield {"event": "chunk", "data": json.dumps({"text": text})}
                    elif kind == "usage":
                        usage_payload = envelope.get("usage") or {}
                    elif kind == "error":
                        error_msg = str(envelope.get("error") or "unknown")[:200]
                        break
                    elif kind == "done":
                        break
            except Exception as exc:  # pragma: no cover — defensive
                error_msg = f"generator: {exc}"[:200]

            # Resolve the final cost / source / fallback signals, mirror
            # the JSON endpoint's accounting so the UI sees the same
            # numbers as a non-streaming call would have produced.
            usd_cost: float | None = None
            source = "deepseek"
            fallback_reason: str | None = None
            if error_msg or not collected_text:
                # Stream out the local narrative as a fallback so the
                # hero is never blank, even when DeepSeek errored mid-call.
                source = "local"
                fallback_reason = error_msg or "no_content"
                remaining = local
                if collected_text:
                    # If we already emitted partial DeepSeek text, append
                    # a separator before the local fallback so the user
                    # sees the discontinuity rather than a jumbled blend.
                    sep = "\n\n[network interrupted — switching to local synthesis]\n\n"
                    yield {"event": "chunk", "data": json.dumps({"text": sep})}
                for token in _word_chunks(remaining):
                    yield {"event": "chunk", "data": json.dumps({"text": token})}
                    await asyncio.sleep(0)
            else:
                if usage_payload:
                    usd_cost = client.estimate_usd(
                        input_tokens=int(usage_payload.get("prompt_tokens") or 0),
                        output_tokens=int(usage_payload.get("completion_tokens") or 0),
                    )
                    sup.reviewers.add_spend(usd_cost)

            done_payload: dict[str, Any] = {
                "ok": True,
                "source": source,
                "generated_at": _dt.now(UTC).isoformat(),
            }
            if usd_cost is not None:
                done_payload["usd_cost"] = usd_cost
            if fallback_reason is not None:
                done_payload["fallback_reason"] = fallback_reason
            yield {"event": "done", "data": json.dumps(done_payload)}

        return EventSourceResponse(
            gen(),
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/quant/invalidate")
    def quant_invalidate() -> dict[str, Any]:
        """Drop the engine's in-memory cache. Wired to news-poller post-ingest."""
        engine = _get_quant_engine()
        engine.invalidate()
        return {"ok": True}

    @app.get("/api/quant/sentiment-momentum")
    def quant_sentiment_momentum(
        limit: int = Query(1000, ge=10, le=5000),
        bucket_minutes: int = Query(240, ge=5, le=1440),
        min_mentions: int = Query(4, ge=1, le=100),
        max_tickers: int = Query(25, ge=1, le=200),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.sentiment_momentum(
            limit=limit,
            bucket_minutes=bucket_minutes,
            min_mentions=min_mentions,
            max_tickers=max_tickers,
        )
        if report is None:
            return {"bucket_minutes": bucket_minutes, "min_mentions": min_mentions, "tickers": []}
        return asdict(report)

    @app.get("/api/quant/sentiment-dispersion")
    def quant_sentiment_dispersion(
        limit: int = Query(1000, ge=10, le=5000),
        scope: str = Query(
            "asset_classes",
            pattern="^(overall|asset_classes|candidate_symbols)$",
        ),
    ) -> dict[str, Any]:
        """Shannon-entropy dispersion across pos/neu/neg sentiment labels.

        ``scope=overall`` returns a single ``result`` envelope; the
        per-bucket scopes return a ``buckets`` array capped at the top
        30 by sample size. ``counts`` is always the canonical
        ``{positive, neutral, negative}`` shape so the UI can index
        without defensive coding.
        """

        from .quant.sentiment_dispersion import (
            compute_by_scope,
            compute_dispersion,
        )

        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        generated_at = _dt.now(UTC).isoformat()

        if scope == "overall":
            sentiments = [r.get("sentiment_label") for r in records]
            result = compute_dispersion(sentiments)
            return {
                "schema_version": 1,
                "generated_at": generated_at,
                "scope": "overall",
                "sample_window": len(records),
                "result": {
                    "scope": result.scope,
                    "sample_size": result.sample_size,
                    "counts": result.counts,
                    "entropy": result.entropy,
                    "max_entropy": result.max_entropy,
                    "normalized_entropy": result.normalized_entropy,
                    "dominant_label": result.dominant_label,
                },
                "buckets": None,
            }

        bucket_results = compute_by_scope(list(records), scope_key=scope)
        return {
            "schema_version": 1,
            "generated_at": generated_at,
            "scope": scope,
            "sample_window": len(records),
            "result": None,
            "buckets": [
                {
                    "scope": b.scope,
                    "sample_size": b.sample_size,
                    "counts": b.counts,
                    "entropy": b.entropy,
                    "max_entropy": b.max_entropy,
                    "normalized_entropy": b.normalized_entropy,
                    "dominant_label": b.dominant_label,
                }
                for b in bucket_results[:30]
            ],
        }

    @app.get("/api/quant/intensity")
    def quant_intensity(
        limit: int = Query(2000, ge=10, le=5000),
        scope: str = Query(
            "asset_classes",
            pattern="^(overall|asset_classes|candidate_symbols)$",
        ),
    ) -> dict[str, Any]:
        """Sentiment intensity = ``relevance * |sentiment_score|``.

        ``scope=overall`` returns a single ``result`` envelope; the per-
        bucket scopes return a ``buckets`` list capped at the top 20 by
        ``mean_intensity`` DESC. Each entry carries ``top_records`` (≤5)
        so the UI can render a click-to-expand drill-down without a
        second round-trip.
        """

        from .quant.intensity import compute_by_scope, compute_overall

        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        generated_at = _dt.now(UTC).isoformat()

        if scope == "overall":
            bucket = compute_overall(list(records))
            return {
                "schema_version": 1,
                "generated_at": generated_at,
                "scope": "overall",
                "sample_window": len(records),
                "result": {
                    "scope": bucket.scope,
                    "sample_size": bucket.sample_size,
                    "mean_intensity": bucket.mean_intensity,
                    "max_intensity": bucket.max_intensity,
                    "count_high_intensity": bucket.count_high_intensity,
                    "top_records": bucket.top_records,
                },
                "buckets": None,
            }

        bucket_results = compute_by_scope(list(records), scope_key=scope)[:20]
        return {
            "schema_version": 1,
            "generated_at": generated_at,
            "scope": scope,
            "sample_window": len(records),
            "result": None,
            "buckets": [
                {
                    "scope": b.scope,
                    "sample_size": b.sample_size,
                    "mean_intensity": b.mean_intensity,
                    "max_intensity": b.max_intensity,
                    "count_high_intensity": b.count_high_intensity,
                    "top_records": b.top_records,
                }
                for b in bucket_results
            ],
        }

    @app.get("/api/quant/co-occurrence")
    def quant_co_occurrence(
        limit: int = Query(1000, ge=10, le=5000),
        min_edge_weight: int = Query(2, ge=1, le=100),
        top_n_cells: int = Query(50, ge=1, le=500),
        top_n_edges: int = Query(60, ge=1, le=500),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.co_occurrence(
            limit=limit,
            min_edge_weight=min_edge_weight,
            top_n_cells=top_n_cells,
            top_n_edges=top_n_edges,
        )
        if report is None:
            return {
                "total_records": 0, "distinct_assets": 0, "distinct_reasons": 0, "distinct_symbols": 0,
                "asset_reason_cells": [], "strong_edges": [], "asset_concentration": [],
            }
        return asdict(report)

    @app.get("/api/quant/anomalies")
    def quant_anomalies(
        limit: int = Query(1500, ge=10, le=5000),
        bucket_minutes: int = Query(30, ge=5, le=1440),
        window_buckets: int = Query(12, ge=3, le=200),
        z_threshold: float = Query(2.0, ge=0.5, le=10.0),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.anomalies(
            limit=limit,
            bucket_minutes=bucket_minutes,
            window_buckets=window_buckets,
            z_threshold=z_threshold,
        )
        if report is None:
            return {
                "bucket_minutes": bucket_minutes, "window_buckets": window_buckets, "z_threshold": z_threshold,
                "volume_anomalies": [], "sentiment_shocks": [], "symbol_bursts": [],
            }
        return asdict(report)

    @app.get("/api/quant/spillover")
    def quant_spillover(
        limit: int = Query(1500, ge=10, le=5000),
        bucket_minutes: int = Query(30, ge=5, le=1440),
        lag_buckets: int = Query(1, ge=1, le=10),
        surge_z_threshold: float = Query(1.5, ge=0.5, le=10.0),
    ) -> dict[str, Any]:
        from dataclasses import asdict
        engine = _get_quant_engine()
        report = engine.spillover(
            limit=limit,
            bucket_minutes=bucket_minutes,
            lag_buckets=lag_buckets,
            surge_z_threshold=surge_z_threshold,
        )
        if report is None:
            return {
                "bucket_minutes": bucket_minutes, "lag_buckets": lag_buckets,
                "surge_z_threshold": surge_z_threshold, "edges": [], "self_loops": [], "total_buckets": 0,
            }
        return asdict(report)

    @app.get("/api/quant/symbol-correlation")
    def quant_symbol_correlation(
        limit: int = Query(2000, ge=10, le=5000),
        bucket_minutes: int = Query(60, ge=15, le=1440),
        min_mentions: int = Query(3, ge=1, le=100),
        top_n: int = Query(30, ge=5, le=100),
    ) -> dict[str, Any]:
        """Pearson r between every eligible symbol pair over per-bucket mention counts.

        Bucketing is epoch-anchored (same convention as spillover) so
        successive calls over overlapping windows share boundaries.
        Returns top-N pairs by |r| — both directions (strong positive
        AND strong negative) are interesting to an analyst.
        """
        from .quant.symbol_correlation import compute_pairs

        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        pairs = compute_pairs(
            records,
            bucket_minutes=bucket_minutes,
            min_mentions=min_mentions,
            top_n=top_n,
        )
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            "bucket_minutes": bucket_minutes,
            "min_mentions": min_mentions,
            "pairs": [
                {
                    "symbol_a": p.symbol_a,
                    "symbol_b": p.symbol_b,
                    "pearson_r": p.pearson_r,
                    "n_buckets": p.n_buckets,
                    "a_total": p.a_total,
                    "b_total": p.b_total,
                }
                for p in pairs
            ],
        }

    @app.get("/api/quant/news-velocity")
    def quant_news_velocity(
        limit: int = Query(2000, ge=10, le=10000),
        bucket_minutes: int = Query(5, ge=1, le=60),
        window_minutes: int = Query(60, ge=10, le=1440),
    ) -> dict[str, Any]:
        """News velocity — arrival rate + EMA fast/slow + acceleration z-score.

        Buckets recent records into ``bucket_minutes`` slots over the
        trailing ``window_minutes`` window, fills 0-arrival buckets,
        and reports the per-minute rate, both EMAs (α=0.3 / α=0.05),
        the median baseline + stdev, and a regime label keyed on the
        ``acceleration_z`` z-score.
        """
        from .quant.news_velocity import compute_velocity

        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        report = compute_velocity(
            records,
            bucket_minutes=bucket_minutes,
            window_minutes=window_minutes,
        )
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            "bucket_minutes": bucket_minutes,
            "window_minutes": window_minutes,
            "current_rate_per_min": report.current_rate_per_min,
            "ema_fast": report.ema_fast,
            "ema_slow": report.ema_slow,
            "baseline_rate": report.baseline_rate,
            "baseline_std": report.baseline_std,
            "acceleration_z": report.acceleration_z,
            "regime": report.regime,
            "samples": report.samples,
        }

    @app.get("/api/quant/market-time")
    def quant_market_time(limit: int = Query(1000, ge=10, le=5000)) -> dict[str, Any]:
        """Cluster news arrivals into US equity market sessions.

        Buckets records by published-time (ET) into pre_open / open /
        lunch / close / after_hours / overnight / weekend, then reports
        volume + average finance-relevance + ≥0.5 relevant-count per
        bucket. ``highest_score_session`` and ``highest_volume_session``
        give the UI a one-line headline.
        """
        from dataclasses import asdict as _asdict

        from .quant.market_time import SESSIONS, aggregate_by_session

        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        buckets = aggregate_by_session(records)
        total = sum(b.volume for b in buckets)

        non_empty = [b for b in buckets if b.volume > 0]
        max_score = max(non_empty, key=lambda b: b.avg_score) if non_empty else None
        max_volume = max(non_empty, key=lambda b: b.volume) if non_empty else None

        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            "total_records": total,
            "sessions": list(SESSIONS),
            "buckets": [_asdict(b) for b in buckets],
            "highest_score_session": max_score.session if max_score else None,
            "highest_volume_session": max_volume.session if max_volume else None,
        }

    @app.get("/api/quant/arrival-heatmap")
    def quant_arrival_heatmap(
        limit: int = Query(2000, ge=10, le=10000),
        timezone: str = Query(
            "America/New_York", min_length=3, max_length=64
        ),
    ) -> dict[str, Any]:
        """24h × 7day news-arrival grid anchored to the given timezone.

        Returns a dense 168-cell grid (7 weekdays × 24 hours) the UI can
        render as an ECharts heatmap without densifying client-side.
        ``peak_cells`` lists up to 5 cells tied for the maximum count so
        the analyst can read the highest-flow buckets at a glance.
        """
        from .quant.arrival_heatmap import compute_heatmap

        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        result = compute_heatmap(records, timezone=timezone)
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            **result,
        }

    @app.get("/api/quant/record/{capture_id}/detail")
    def quant_record_detail(capture_id: str) -> dict[str, Any]:
        """Full record + paired second-opinion reviews — drill-down endpoint.

        Returns the primary FinancialImpactRecord, ALL persisted reviewer
        rows for it (stub + DeepSeek if sampled), and the market-reaction
        report. Single endpoint so the UI can render a drawer in one
        round-trip instead of fan-out.
        """
        sup = _get_supervisor()
        rec = sup.storage.get_record(capture_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"unknown capture_id: {capture_id}")
        reviews = sup.storage.get_reviews_for_capture(capture_id)
        # Build a minimal AwarenessCaptureView for reaction lookup.
        try:
            engine = _get_quant_engine()
            reaction = engine.reaction_for(capture_id)
            reaction_payload = (
                {
                    "capture_id": reaction.capture_id,
                    "published_ts": reaction.published_ts,
                    "horizons": [
                        {
                            "horizon": h.horizon,
                            "symbol": h.symbol,
                            "last_at_t0": h.last_at_t0,
                            "last_at_t": h.last_at_t,
                            "return_pct": h.return_pct,
                            "benchmark_return_pct": h.benchmark_return_pct,
                            "excess_return_pct": h.excess_return_pct,
                        }
                        for h in reaction.horizons
                    ],
                    "headline_excess_return_15m": reaction.headline_excess_return_15m,
                    "benchmark_symbol": reaction.benchmark_symbol,
                    "fallback_reason": reaction.fallback_reason,
                }
                if reaction
                else None
            )
        except Exception:
            reaction_payload = None
        return {
            "record": rec,
            "reviews": reviews,
            "reaction": reaction_payload,
        }

    @app.get("/api/quant/cluster/{cluster_id}/members")
    def quant_cluster_members(cluster_id: str, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        """Records belonging to a cluster — used by the drill-down drawer."""
        engine = _get_quant_engine()
        # Pull a recent corpus large enough to recover the cluster's window.
        clusters = engine.clusters(limit=2000)
        target = next((c for c in clusters if c.cluster_id == cluster_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"unknown cluster_id: {cluster_id}")
        sup = _get_supervisor()
        members: list[dict[str, Any]] = []
        for cap_id in target.capture_ids[:limit]:
            rec = sup.storage.get_record(cap_id)
            if rec is None:
                continue
            members.append(
                {
                    "capture_id": rec["capture_id"],
                    "title": rec.get("title"),
                    "domain": rec.get("domain"),
                    "url": rec.get("url"),
                    "published_ts": rec.get("published_ts"),
                    "finance_relevance_score": rec.get("finance_relevance_score"),
                    "sentiment_label": rec.get("sentiment_label"),
                    "asset_classes": rec.get("asset_classes") or [],
                    "impact_reason_codes": rec.get("impact_reason_codes") or [],
                    "candidate_symbols": rec.get("candidate_symbols") or [],
                }
            )
        return {
            "cluster_id": cluster_id,
            "total_in_cluster": len(target.capture_ids),
            "returned": len(members),
            "members": members,
        }

    @app.get("/api/quant/heatmap/records")
    def quant_heatmap_records(
        asset: str = Query(..., min_length=1),
        reason: str = Query(..., min_length=1),
        limit: int = Query(20, ge=1, le=200),
    ) -> dict[str, Any]:
        """Records carrying a specific asset_class × reason_code combo.

        Used by the co-occurrence heatmap click handler — gives the analyst
        the actual stories sitting behind a lift cell.
        """
        sup = _get_supervisor()
        # Use the inverted index to fetch by asset, then filter by reason in-Python.
        # (record_labels has separate entries for asset/reason; no SQL join here.)
        candidates = sup.storage.by_label("asset_class", asset, limit=limit * 4)
        out: list[dict[str, Any]] = []
        for rec in candidates:
            reasons = rec.get("impact_reason_codes") or []
            if reason not in reasons:
                continue
            out.append(
                {
                    "capture_id": rec["capture_id"],
                    "title": rec.get("title"),
                    "domain": rec.get("domain"),
                    "url": rec.get("url"),
                    "published_ts": rec.get("published_ts"),
                    "finance_relevance_score": rec.get("finance_relevance_score"),
                    "sentiment_label": rec.get("sentiment_label"),
                    "candidate_symbols": rec.get("candidate_symbols") or [],
                }
            )
            if len(out) >= limit:
                break
        return {
            "asset_class": asset,
            "reason_code": reason,
            "total_returned": len(out),
            "records": out,
        }

    @app.post("/api/quant/explain")
    def quant_explain(body: dict = Body(...)) -> dict[str, Any]:
        """DeepSeek-narrated explanation of any quant signal payload.

        Body shape: `{"kind": "cluster" | "regime_shift" | "anomaly" | "spillover", "payload": {...}}`.
        Routes the payload through the DeepSeek reviewer (if configured) and
        returns a 1-3 sentence interpretive narrative. Fails open (returns a
        sensible local explanation if DeepSeek is unavailable) so the UI never
        blocks on a network call.
        """
        sup = _get_supervisor()
        kind = str(body.get("kind") or "").strip().lower()
        payload = body.get("payload") or {}
        if not kind:
            raise HTTPException(status_code=422, detail="kind is required")
        local = _local_explain(kind, payload)
        client = sup.reviewers.deepseek()
        # Budget guard: don't burn DeepSeek tokens on every signal click.
        if client is None or sup.reviewers.budget_state().exhausted:
            return {"kind": kind, "narrative": local, "source": "local"}
        prompt_body = _build_explain_prompt(kind, payload, local)
        # Use the DeepSeek client directly; we don't persist this in `reviews`
        # because it's a narrative, not a review row.
        try:
            import httpx as _httpx
            req = {
                "model": client.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a financial-news analyst explaining a quant signal. "
                            "Write 1-3 short sentences. No bullet points, no preamble. "
                            "Be precise — refer to specific numbers/tickers in the payload."
                        ),
                    },
                    {"role": "user", "content": prompt_body},
                ],
                "temperature": 0.3,
                "max_tokens": 220,
            }
            response = client._client.post(  # noqa: SLF001 — reuse the configured httpx.Client
                f"{client._base_url}/chat/completions",  # noqa: SLF001
                json=req,
                headers={"Authorization": f"Bearer {client._api_key}", "Content-Type": "application/json"},  # noqa: SLF001
            )
            if response.status_code != 200:
                return {"kind": kind, "narrative": local, "source": "local", "fallback_reason": f"http_{response.status_code}"}
            envelope = response.json()
            content = (envelope.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            usage = envelope.get("usage") or {}
            usd = client.estimate_usd(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
            sup.reviewers.add_spend(usd)
            return {"kind": kind, "narrative": content or local, "source": "deepseek", "usd_cost": usd}
        except Exception as exc:
            return {"kind": kind, "narrative": local, "source": "local", "fallback_reason": str(exc)[:120]}

    # ────────────────────────────────────────────────────────────────────────
    # Global content search — backs the ⌘P palette in the SPA.
    #
    # Linear scan over the recent 500 records, top 100 symbols, and up to
    # 200 fresh clusters. Substring match (case-insensitive); no fuzzy
    # scoring, no fts5 index. At catchem's scale (≤ a few k records in
    # SQLite) this is well under 30ms and avoids paying the index cost.
    #
    # Distinct from CommandPalette (⌘K): that one is nav+imperative
    # actions. This one searches *content* — titles, domains, ticker
    # mentions, cluster_ids/dominant_symbols.
    # ────────────────────────────────────────────────────────────────────────

    @app.get("/api/search", dependencies=[Depends(_rate_limit_search)])
    def api_search(
        q: str = Query(..., min_length=2, max_length=128, description="Free-text query"),
        limit: int = Query(20, ge=1, le=50),
    ) -> dict[str, Any]:
        """Free-text search across records (title + domain), symbols (mention
        frequency), and clusters (cluster_id prefix + dominant symbols).

        Linear over recent 500 records; ≤ 30ms at catchem's scale.
        Production-safe redaction is applied to record rows before the title
        substring match so we never leak a redacted title back through search.
        """
        sup = _get_supervisor()
        prod_safe = _is_production_safe()
        q_lower = q.strip().lower()
        if not q_lower:
            raise HTTPException(status_code=422, detail="q must be non-empty")

        # 1. Records: title + domain substring (relevant_only=False so the
        #    palette finds anything that was ingested, not just the
        #    finance-relevant subset).
        records_raw = sup.storage.recent_records(limit=500, relevant_only=False)
        records_view = redact_records_for_mode(records_raw, production_safe=prod_safe)
        matched_records: list[dict[str, Any]] = []
        for r in records_view:
            title = (r.get("title") or "").lower()
            domain = (r.get("domain") or "").lower()
            if q_lower in title or q_lower in domain:
                matched_records.append({
                    "capture_id": r.get("capture_id"),
                    "title": r.get("title"),
                    "domain": r.get("domain"),
                    "score": r.get("finance_relevance_score"),
                    "published_ts": r.get("published_ts"),
                })
                if len(matched_records) >= limit:
                    break

        # 2. Symbols: top mentions over the same recent corpus.
        sym_counter: Counter[str] = Counter()
        for r in records_raw:
            for s in r.get("candidate_symbols", []):
                sym_counter[s] += 1
        matched_symbols: list[dict[str, Any]] = []
        for sym, count in sym_counter.most_common(100):
            if q_lower in sym.lower():
                matched_symbols.append({"symbol": sym, "count": int(count)})
                if len(matched_symbols) >= limit:
                    break

        # 3. Clusters: cluster_id prefix or any dominant_symbol substring.
        matched_clusters: list[dict[str, Any]] = []
        try:
            engine = _get_quant_engine()
            cs = engine.clusters(limit=2000)
        except Exception:  # noqa: BLE001 — quant engine optional
            cs = []
        for c in cs[:200]:
            cid = (c.cluster_id or "").lower()
            symbols = tuple(c.dominant_symbols or ())
            if q_lower in cid or any(q_lower in (s or "").lower() for s in symbols):
                matched_clusters.append({
                    "cluster_id": c.cluster_id,
                    "size": int(c.size),
                    "symbols": list(symbols),
                })
                if len(matched_clusters) >= limit:
                    break

        return {
            "query": q,
            "records": matched_records,
            "symbols": matched_symbols,
            "clusters": matched_clusters,
        }

    # ────────────────────────────────────────────────────────────────────────
    # Analyst workflow — filtered CSV / JSON export of records, paired reviews,
    # and quant signals. Read-only, redacted in production_safe mode, and
    # backed by storage.recent_records() — no new SQL paths to maintain.
    # ────────────────────────────────────────────────────────────────────────

    # Fields surfaced in the records CSV. Order is load-bearing — analysts
    # eyeball the column header so the most-scanned columns come first.
    _EXPORT_RECORD_FIELDS = (
        "capture_id", "title", "domain", "url", "published_ts", "created_at",
        "is_finance_relevant", "finance_relevance_score",
        "sentiment_label", "sentiment_score",
        "asset_classes", "impact_reason_codes", "candidate_symbols",
        "processing_mode",
    )

    def _filter_records_for_export(
        records: list[dict[str, Any]],
        asset_class: str | None,
        reason_code: str | None,
        symbol: str | None,
        min_score: float | None,
    ) -> list[dict[str, Any]]:
        """Apply analyst filter chips to a recent-records list.

        Keeps the api.py route bodies short and shares the same predicate
        with the /api/export/reviews endpoint (filters reviews by their
        associated record's labels).
        """
        out = records
        if asset_class:
            out = [r for r in out if asset_class in (r.get("asset_classes") or [])]
        if reason_code:
            out = [r for r in out if reason_code in (r.get("impact_reason_codes") or [])]
        if symbol:
            out = [r for r in out if symbol in (r.get("candidate_symbols") or [])]
        if min_score is not None:
            out = [r for r in out if (r.get("finance_relevance_score") or 0.0) >= min_score]
        return out

    def _records_csv(records: list[dict[str, Any]]) -> str:
        """Serialize records to CSV with list-fields joined by ';'.

        csv.DictWriter handles the embedded-comma / newline / quote
        escaping; we just need to flatten the list-typed columns.
        """
        import csv
        from io import StringIO
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(_EXPORT_RECORD_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row: dict[str, Any] = {k: r.get(k) for k in _EXPORT_RECORD_FIELDS}
            for list_field in ("asset_classes", "impact_reason_codes", "candidate_symbols"):
                v = row.get(list_field)
                if isinstance(v, list):
                    row[list_field] = ";".join(str(x) for x in v)
            writer.writerow(row)
        return buf.getvalue()

    def _attachment_headers(filename: str) -> dict[str, str]:
        return {"Content-Disposition": f'attachment; filename="{filename}"'}

    @app.get("/api/export/records", dependencies=[Depends(_rate_limit_db_export)])
    def api_export_records(
        format: str = Query("json", pattern="^(csv|json)$"),
        asset_class: str | None = Query(None),
        reason_code: str | None = Query(None),
        symbol: str | None = Query(None),
        min_score: float | None = Query(None, ge=0.0, le=1.0),
        limit: int = Query(500, ge=1, le=5000),
    ) -> Response:
        """Filtered CSV/JSON export of FinancialImpactRecord rows.

        Reuses ``storage.recent_records`` so we don't introduce a second
        SQL surface. Diagnostic fields are scrubbed in production_safe mode
        through the same redactor as the live /recent endpoint.
        """
        sup = _get_supervisor()
        prod_safe = _is_production_safe()
        raw = sup.storage.recent_records(limit=limit, relevant_only=False)
        records = redact_records_for_mode(raw, production_safe=prod_safe)
        records = _filter_records_for_export(records, asset_class, reason_code, symbol, min_score)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        if format == "json":
            payload = {
                "exported_at": datetime.now(UTC).isoformat(),
                "count": len(records),
                "filters": {
                    "asset_class": asset_class,
                    "reason_code": reason_code,
                    "symbol": symbol,
                    "min_score": min_score,
                    "limit": limit,
                },
                "items": records,
            }
            return Response(
                json.dumps(payload, default=str, indent=2),
                media_type="application/json",
                headers=_attachment_headers(f"catchem_records_{stamp}.json"),
            )
        return Response(
            _records_csv(records),
            media_type="text/csv",
            headers=_attachment_headers(f"catchem_records_{stamp}.csv"),
        )

    _EXPORT_REVIEW_FIELDS = (
        "capture_id", "title", "domain", "url",
        "stub_relevance", "stub_score", "stub_sentiment",
        "stub_assets", "stub_reasons", "stub_symbols",
        "ds_relevance", "ds_score", "ds_sentiment",
        "ds_assets", "ds_reasons", "ds_symbols",
        "ds_error_code", "ds_usd_cost", "ds_latency_ms",
        "ds_created_at",
        "agreement_overall", "agreement_relevance_match",
        "agreement_sentiment_match", "agreement_score_delta",
        "agreement_asset_jaccard", "agreement_reason_jaccard",
        "agreement_symbol_jaccard",
    )

    def _flatten_review_pair(item: dict[str, Any]) -> dict[str, Any]:
        """Project a /api/reviews/compare row down to one flat CSV row."""
        stub = (item.get("stub") or {}).get("payload") or {}
        ds_row = item.get("deepseek") or {}
        ds = ds_row.get("payload") or {}
        ag = item.get("agreement") or {}

        def _join(v: Any) -> str:
            return ";".join(str(x) for x in v) if isinstance(v, list) else ""

        return {
            "capture_id": item.get("capture_id"),
            "title": item.get("title"),
            "domain": item.get("domain"),
            "url": item.get("url"),
            "stub_relevance": stub.get("is_finance_relevant"),
            "stub_score": stub.get("finance_relevance_score"),
            "stub_sentiment": stub.get("sentiment_label"),
            "stub_assets": _join(stub.get("asset_classes")),
            "stub_reasons": _join(stub.get("impact_reason_codes")),
            "stub_symbols": _join(stub.get("candidate_symbols")),
            "ds_relevance": ds.get("is_finance_relevant"),
            "ds_score": ds.get("finance_relevance_score"),
            "ds_sentiment": ds.get("sentiment_label"),
            "ds_assets": _join(ds.get("asset_classes")),
            "ds_reasons": _join(ds.get("impact_reason_codes")),
            "ds_symbols": _join(ds.get("candidate_symbols")),
            "ds_error_code": ds_row.get("error_code"),
            "ds_usd_cost": ds_row.get("usd_cost"),
            "ds_latency_ms": ds_row.get("latency_ms"),
            "ds_created_at": ds_row.get("created_at"),
            "agreement_overall": ag.get("overall"),
            "agreement_relevance_match": ag.get("relevance_match"),
            "agreement_sentiment_match": ag.get("sentiment_match"),
            "agreement_score_delta": ag.get("score_delta"),
            "agreement_asset_jaccard": ag.get("asset_jaccard"),
            "agreement_reason_jaccard": ag.get("reason_jaccard"),
            "agreement_symbol_jaccard": ag.get("symbol_jaccard"),
        }

    @app.get("/api/export/reviews", dependencies=[Depends(_rate_limit_db_export)])
    def api_export_reviews(
        format: str = Query("json", pattern="^(csv|json)$"),
        asset_class: str | None = Query(None),
        reason_code: str | None = Query(None),
        symbol: str | None = Query(None),
        min_score: float | None = Query(None, ge=0.0, le=1.0),
        limit: int = Query(500, ge=1, le=5000),
    ) -> Response:
        """Filtered export of paired (stub, deepseek) review rows.

        Filters apply to the *stub* payload's labels so a "min_score >= 0.5"
        cut means "captures the stub already scored as material".
        """
        sup = _get_supervisor()
        pairs = sup.storage.reviews_with_pair("stub", "deepseek", limit=limit)
        items: list[dict[str, Any]] = []
        for stub_row, ds_row in pairs:
            cap_id = stub_row["capture_id"]
            rec = sup.storage.get_record(cap_id) or {}
            items.append(
                {
                    "capture_id": cap_id,
                    "title": rec.get("title"),
                    "domain": rec.get("domain"),
                    "url": rec.get("url"),
                    "stub": stub_row,
                    "deepseek": ds_row,
                    "agreement": _compute_agreement(stub_row["payload"], ds_row["payload"]),
                }
            )

        # Filter by the stub payload's labels (mirrors what the compare UI
        # shows the analyst on screen). DeepSeek-only filtering would be a
        # different cut and is intentionally not what we expose here.
        def _stub_view(it: dict[str, Any]) -> dict[str, Any]:
            return (it.get("stub") or {}).get("payload") or {}

        if asset_class:
            items = [it for it in items if asset_class in (_stub_view(it).get("asset_classes") or [])]
        if reason_code:
            items = [it for it in items if reason_code in (_stub_view(it).get("impact_reason_codes") or [])]
        if symbol:
            items = [it for it in items if symbol in (_stub_view(it).get("candidate_symbols") or [])]
        if min_score is not None:
            items = [it for it in items if (_stub_view(it).get("finance_relevance_score") or 0.0) >= min_score]

        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        if format == "json":
            payload = {
                "exported_at": datetime.now(UTC).isoformat(),
                "count": len(items),
                "filters": {
                    "asset_class": asset_class,
                    "reason_code": reason_code,
                    "symbol": symbol,
                    "min_score": min_score,
                    "limit": limit,
                },
                "items": items,
            }
            return Response(
                json.dumps(payload, default=str, indent=2),
                media_type="application/json",
                headers=_attachment_headers(f"catchem_reviews_{stamp}.json"),
            )
        # CSV flattens both reviewer payloads + agreement onto one row.
        import csv
        from io import StringIO
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(_EXPORT_REVIEW_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for it in items:
            writer.writerow(_flatten_review_pair(it))
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers=_attachment_headers(f"catchem_reviews_{stamp}.csv"),
        )

    @app.get("/api/export/quant", dependencies=[Depends(_rate_limit_db_export)])
    def api_export_quant(
        format: str = Query("json", pattern="^(csv|json)$"),
        limit: int = Query(1000, ge=10, le=5000),
    ) -> Response:
        """Structured quant-signal export for offline analysis.

        JSON only — the payload is nested (clusters with member arrays,
        spillover edge tuples, anomaly bursts) and would lose its shape
        in a flat CSV. CSV requests get a 415 with a clear hint.
        """
        if format == "csv":
            raise HTTPException(
                status_code=415,
                detail="quant signals are nested; export as JSON instead",
            )
        engine = _get_quant_engine()
        snap = engine.dashboard_snapshot(limit=limit)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        payload = {
            "exported_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            "signals": snap,
        }
        return Response(
            json.dumps(payload, default=str, indent=2),
            media_type="application/json",
            headers=_attachment_headers(f"catchem_quant_{stamp}.json"),
        )

    # ────────────────────────────────────────────────────────────────────────
    # Database backup / export / import
    # Lets the operator pull the SQLite truth-store off the host as a single
    # file, or push a previously-saved snapshot back in. Import auto-creates
    # a timestamped backup of the current DB before overwriting, so this is
    # safe even when the operator confuses two snapshots — the worst case is
    # a forced backup file they can rename back.
    #
    # All three endpoints intentionally surface real filesystem paths to the
    # caller (the local UI lives on 127.0.0.1, never exposed). The display
    # name is tilde-redacted via `_display_path` to avoid leaking `/Users/…`
    # into screenshots; the import response also redacts.
    # ────────────────────────────────────────────────────────────────────────

    # SQLite file format magic header — first 16 bytes of every valid DB.
    # Hard-coded so the validator works without importing sqlite3 (cheaper
    # than spinning up a connection just to ping-test the file). Reject
    # anything that doesn't start with this — text, zip, random binary,
    # the wrong sqlite *version* (which still starts with this string in
    # both v3 and the forks we care about).
    _SQLITE_MAGIC = b"SQLite format 3\x00"

    @app.get("/api/db/info")
    def db_info() -> dict[str, Any]:
        """Metadata about the live SQLite truth-store file."""
        s = _SETTINGS or load_settings()
        db_path = s.sqlite_path()
        if not db_path.exists():
            return {"exists": False, "path": _display_path(db_path)}
        st = db_path.stat()
        return {
            "exists": True,
            "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            "path": _display_path(db_path),
            # Generated_at lets the UI tell stale info polls apart from
            # never-loaded state — handy when the file gets touched by
            # another process between renders.
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/db/stats", dependencies=[Depends(_rate_limit_db_export)])
    def db_stats() -> dict[str, Any]:
        """Per-table SQLite row counts + index summary.

        Useful for ops: an unexpected row count growth in dlq or a missing
        record_tags table (pre-v38) is immediately visible. Each table query
        is `COUNT(*)` over the live connection — cheap on a WAL DB.
        """
        sup = _get_supervisor()
        storage = sup.storage
        tables: list[dict[str, Any]] = []
        with storage._lock, storage._connection() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            table_names = [r[0] for r in rows]
            for name in table_names:
                # SQLite doesn't parameterize table names — but identifiers
                # CAN be double-quoted (per SQL spec). Strip any embedded
                # quotes from the name first so a maliciously-named table
                # (e.g. `tbl"; DROP TABLE records; --`) can't escape the
                # quoted identifier. Names from sqlite_master are usually
                # clean but defense-in-depth beats trust.
                safe_name = name.replace('"', '')
                try:
                    count = conn.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0]
                except Exception:
                    count = -1  # query failure, surface explicitly
                tables.append({"name": name, "rows": int(count)})
            # Index list (name + table)
            idx_rows = conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name, name"
            ).fetchall()
            indexes = [{"name": r[0], "table": r[1]} for r in idx_rows]
            page_count_row = conn.execute("PRAGMA page_count").fetchone()
            page_size_row = conn.execute("PRAGMA page_size").fetchone()
            page_count = int(page_count_row[0]) if page_count_row else 0
            page_size = int(page_size_row[0]) if page_size_row else 0

        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "tables": tables,
            "indexes": indexes,
            "total_tables": len(tables),
            "total_indexes": len(indexes),
            "page_count": page_count,
            "page_size_bytes": page_size,
            "estimated_size_bytes": page_count * page_size,
        }

    @app.get("/api/db/schema_version")
    def db_schema_version() -> dict[str, Any]:
        """Return migration state for the live SQLite truth-store.

        ``user_version`` is the PRAGMA value stored in the DB file
        (0 on a never-migrated database). ``max_known`` is the
        highest version declared in :mod:`catchem.migrations`.
        ``migrations_pending`` lists the migrations that would run
        on the next storage init — empty in steady state. The
        Settings → Database card reads this to display a one-line
        "Schema version: N" hint below the DB stats.
        """
        from .migrations import MIGRATIONS, current_version, max_known_version

        sup = _get_supervisor()
        # Acquire the storage RLock before opening a connection so we never
        # race a concurrent writer (insert_record / archive DELETE) that's
        # mid-transaction. PRAGMA user_version reads cheaply, but without
        # the lock a fresh connection on the same DB file could observe a
        # half-applied migration if one runs through migrations on init.
        with sup.storage._lock, sup.storage._connection() as conn:
            current = current_version(conn)
        pending = [m.name for m in MIGRATIONS if m.version > current]
        return {
            "user_version": current,
            "max_known": max_known_version(),
            "migrations_pending": pending,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/db/export", dependencies=[Depends(_rate_limit_db_export)])
    def db_export() -> FileResponse:
        """Stream the SQLite file as a download.

        The `Content-Disposition: attachment` header from `FileResponse`'s
        `filename=` kwarg drives the browser's native "Save as…" prompt,
        which is what the UI's <a href=... download> anchor expects.
        """
        s = _SETTINGS or load_settings()
        db_path = s.sqlite_path()
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="database not found")
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        return FileResponse(
            db_path,
            media_type="application/octet-stream",
            filename=f"catchem_{stamp}.sqlite3",
        )

    @app.post("/api/db/import", dependencies=[Depends(_rate_limit_db_import)])
    async def db_import(file: UploadFile = File(...)) -> dict[str, Any]:
        """Replace the SQLite truth-store with an uploaded snapshot.

        Validates the SQLite magic header BEFORE touching disk. The old
        DB is copied to `catchem_backup_<ts>.sqlite3` in the same
        directory first, so a bad import is a `mv` away from recovery.

        After the file lands, the operator must reload the SPA — the
        live supervisor still holds the previous DB connection.
        """
        # Cap the read at a reasonable size. The default working set is
        # ~300 KB (see archive cap=150 rows); even a huge backup is
        # comfortably under 200 MB. This is the same ceiling
        # `text_extract.MAX_UPLOAD_BYTES` enforces on text uploads.
        content = await file.read()
        if not content:
            raise HTTPException(status_code=422, detail="upload is empty")
        if len(content) > 200 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="upload exceeds 200 MB cap")
        if not content.startswith(_SQLITE_MAGIC):
            raise HTTPException(status_code=400, detail="not a valid SQLite file")

        s = _SETTINGS or load_settings()
        db_path = s.sqlite_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        backup_path: Path | None = None
        if db_path.exists():
            import shutil
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            backup_path = db_path.with_name(f"catchem_backup_{stamp}.sqlite3")
            shutil.copy2(db_path, backup_path)

        # Write atomically: stage to a sibling temp file, fsync, then
        # rename over the live path. Belt-and-braces — even on a power
        # cut mid-write the old file (or backup) is still recoverable.
        #
        # The fsync is critical: `write_bytes` alone leaves the data in
        # the kernel page cache. A crash between write_bytes() and
        # tmp_path.replace() — or between the rename and the next
        # background flush — can leave a zero-length / truncated DB
        # under db_path. We open + fsync the FD explicitly before the
        # rename so the bytes are durably on disk *first*.
        tmp_path = db_path.with_name(f".{db_path.name}.import.tmp")
        tmp_path.write_bytes(content)
        fd = os.open(tmp_path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp_path.replace(db_path)

        return {
            "ok": True,
            "backup_path": _display_path(backup_path) if backup_path else None,
            "imported_size_bytes": len(content),
            "db_path": _display_path(db_path),
            "generated_at": datetime.now(UTC).isoformat(),
        }

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
            use_ml_stubs=s.models.use_ml_stubs,
            diagnostic_allowed=s.diagnostic_allowed(),
            static_bundle_present=bundle is not None,
            model_versions=dict(sup.service.model_versions),
            generated_at=_dt.now(UTC).isoformat(),
        )

    @app.get("/ui/sidecar-status", response_model=SidecarStatusResponse)
    def ui_sidecar_status() -> SidecarStatusResponse:
        sup = _get_supervisor()
        s = _SETTINGS or load_settings()
        counts = sup.storage.count_records()
        uptime = (_dt.now(UTC) - _PROCESS_STARTED_AT).total_seconds()
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
            generated_at=_dt.now(UTC).isoformat(),
        )

    @app.get("/ui/log-tail", response_model=LogTailResponse)
    def ui_log_tail(lines: int = Query(120, ge=1, le=2000)) -> LogTailResponse:
        s = _SETTINGS or load_settings()
        log_rel = s.logging.file
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
            "use_ml_stubs": s.models.use_ml_stubs,
            "totals": dash["totals"],
            "diagnostic_count": 0 if prod_safe else dash["diagnostic_count"],
            "asset_class_distribution": dash["asset_class_distribution"],
            "reason_code_distribution": dash["reason_code_distribution"],
            "sentiment_distribution": dash["sentiment_distribution"],
            "recent_top": redact_records_for_mode(recent_top, production_safe=prod_safe),
            "dlq": sup.storage.dlq_count(),
            "model_versions": dict(sup.service.model_versions),
            "guards": safe_guard_view(guards),
            "generated_at": datetime.now(UTC).isoformat(),
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
            # Epoch-based truncation: works uniformly for any bucket size,
            # including bucket_minutes > 60 (e.g. 120, 240, 1440). The
            # previous `dt.replace(minute=...)` arithmetic raised
            # ValueError whenever bucket_minutes >= 60 because the
            # computed `minute` exceeded the [0..59] range that
            # `replace(minute=)` accepts.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ts_epoch = int(dt.timestamp())
            bucket_seconds = bucket_minutes * 60
            bucket_epoch = ts_epoch - (ts_epoch % bucket_seconds)
            key = datetime.fromtimestamp(bucket_epoch, UTC).isoformat()
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

    @app.get("/ui/quotes", response_model=MarketQuoteBatchResponse)
    def ui_quotes(symbols: str = Query("", description="Comma-separated symbols")) -> MarketQuoteBatchResponse:
        """Batch quote contract.

        Current provider is local fixture-only. Known fixtures return stale
        snapshots; unknown symbols return typed unavailable items instead of
        raising 5xx.
        """
        parsed = parse_symbol_list(symbols)[:50]
        return MarketQuoteBatchResponse(
            items=_MARKET_DATA.quotes(parsed),
            provider=_MARKET_DATA.provider,
            generated_at=datetime.now(UTC).isoformat(),
        )

    @app.get("/ui/quote/{symbol}", response_model=MarketQuote)
    def ui_quote(symbol: str) -> MarketQuote:
        """Single-symbol quote contract with the same stale/unavailable semantics."""
        return _MARKET_DATA.quote(symbol)

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
        """Asset-class x reason-code co-occurrence matrix."""
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
        return {**rep.to_dict(), "ran_at": datetime.now(UTC).isoformat()}

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

    @app.get("/api/symbols/{symbol}/sentiment-trend")
    def api_symbol_sentiment_trend(
        symbol: str,
        days: int = Query(7, ge=1, le=30),
    ) -> dict[str, Any]:
        """Daily sentiment breakdown for one symbol over the trailing ``days`` window.

        Returns one series row per UTC day in the window (zero-filled where
        no records exist), each carrying integer counts of records labelled
        ``positive`` / ``neutral`` / ``negative``. Powers the stacked-area
        chart on the Symbol Detail page; the same rows drive the mention
        velocity sparkline (sum of the three buckets per day).

        Symbol resolution uses the ``record_labels`` inverted index
        (``kind='symbol'``) — never a LIKE on the JSON column — so the
        match is exact and indexed.
        """
        sup = _get_supervisor()
        storage = sup.storage
        # Build the UTC day window so the same boundaries are used for
        # zero-fill and for the SQL filter. ``today`` is included.
        now_utc = datetime.now(UTC)
        today = now_utc.date()
        window_days = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
        cutoff_iso = datetime.combine(window_days[0], datetime.min.time(), tzinfo=UTC).isoformat()
        with storage._lock, storage._connection() as conn:
            rows = conn.execute(
                """
                SELECT substr(records.published_ts, 1, 10) AS day,
                       records.sentiment_label AS label,
                       COUNT(*) AS cnt
                  FROM records
                  JOIN record_labels
                    ON records.capture_id = record_labels.capture_id
                 WHERE record_labels.kind = 'symbol'
                   AND record_labels.value = ?
                   AND records.published_ts IS NOT NULL
                   AND records.published_ts >= ?
                   AND records.sentiment_label IS NOT NULL
                 GROUP BY day, label
                 ORDER BY day ASC
                """,
                (symbol, cutoff_iso),
            ).fetchall()
        # Zero-filled, ordered per-day buckets — the UI assumes one row per
        # day so the stacked area renders a contiguous time axis even when
        # the symbol has no mentions on a given day.
        by_day: dict[str, dict[str, int]] = {
            d.isoformat(): {"positive": 0, "neutral": 0, "negative": 0}
            for d in window_days
        }
        for row in rows:
            day = row["day"]
            label = row["label"]
            if day in by_day and label in by_day[day]:
                by_day[day][label] = int(row["cnt"])
        return {
            "symbol": symbol,
            "days": days,
            "series": [{"day": d, **counts} for d, counts in by_day.items()],
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
                "backed_off_feeds": 0,
                "feed_health": [],
                "max_item_age_seconds": None,
                "last_stale_skipped": 0,
            }
        feed_health = _NEWS_POLLER.feed_health_snapshot()
        return {
            "enabled": True,
            "feeds": len(_NEWS_POLLER.feeds),
            "interval_seconds": _NEWS_POLLER.interval_seconds,
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
            "backed_off_feeds": sum(1 for f in feed_health if bool(f.get("backed_off"))),
            "feed_health": feed_health,
            "max_item_age_seconds": _NEWS_POLLER.max_item_age_seconds,
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

    @app.post(
        "/api/news/sources/probe",
        dependencies=[Depends(_rate_limit_probe)],
    )
    async def api_probe_source(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """One-shot per-feed probe — bypasses the circuit-breaker cooldown.

        The Sources page's "probe" button calls this with a single URL.
        We verify the URL belongs to a configured feed (otherwise an
        attacker could redirect the sidecar at arbitrary RSS endpoints),
        run one fetch through the same FeedFetchResult pipeline as the
        background loop, and return the freshly-updated feed_health row.

        Returns {"ok": True, "url": ..., "result": <feed_health>} on
        success, or {"ok": False, "url": ..., "error": <str>} when the
        probe itself raised (the http-level error is folded into
        feed_health by the underlying fetcher and the route still
        returns 200 in that case — we only surface ok=False when the
        invocation itself blew up).
        """
        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url required")

        poller = _NEWS_POLLER
        if poller is None:
            raise HTTPException(status_code=503, detail="news_poller_disabled")

        # Membership check: probe only configured feeds. Defends against
        # SSRF (operator-redirected sidecar) + clarifies the 404 vs the
        # 400-on-missing case.
        if not any(spec.url == url for spec in poller.feeds):
            raise HTTPException(status_code=404, detail="feed not configured")

        try:
            result = await poller.probe_feed_async(url)
            return {"ok": True, "url": url, "result": result}
        except Exception as exc:  # never let one bad probe poison the route
            logger.exception("news_probe_failed", url=url)
            return {"ok": False, "url": url, "error": str(exc)}

    @app.get("/api/quant/persistence")
    def api_quant_persistence(
        limit: int = Query(2000, ge=10, le=10000),
        window_days: int = Query(7, ge=1, le=90),
        min_records: int = Query(3, ge=1, le=100),
        top_n: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """News persistence — long-running narratives.

        Counts how many distinct UTC-date buckets each (asset_class, top_symbol)
        scope received mentions in. High ratio (≥0.7) = structural narrative
        the market keeps tracking. Low ratio = one-day spike that died.

        Pairs well with sentiment_dispersion: persistent+aligned narratives
        often precede sustained moves; persistent+disputed narratives sit
        in regime-uncertain zones.
        """
        sup = _get_supervisor()
        records = sup.storage.recent_records(limit, relevant_only=False)
        from .quant.persistence import compute_persistence
        buckets = compute_persistence(
            records,
            window_days=window_days,
            min_records=min_records,
            top_n=top_n,
        )
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            "window_days": window_days,
            "min_records": min_records,
            "buckets": [
                {
                    "scope": b.scope,
                    "days_covered": b.days_covered,
                    "total_records": b.total_records,
                    "persistence_ratio": b.persistence_ratio,
                    "sample_titles": b.sample_titles,
                }
                for b in buckets
            ],
        }

    @app.get("/api/quant/diagnostics")
    def api_quant_diagnostics() -> dict[str, Any]:
        """Recent QuantEngine signal failures — fail-soft observability.

        Each of the 18 quant signals runs through ``_safe_call`` which
        catches any exception, logs a structured warning, returns ``None``,
        and lets the dashboard keep rendering the other signals. That
        graceful degradation was previously invisible to the operator —
        the only trace was a single warning line in the rotating log file.

        This endpoint exposes the last 50 failures (in-process ring buffer)
        with class, message, traceback head, and elapsed-ms-before-failure,
        plus per-signal counts so the UI can show a "N signals degraded"
        chip on the QuantScan hero. Newest failure first.

        Empty payload (`total_failures=0`) is the healthy steady state.
        """
        engine = _get_quant_engine()
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            **engine.diagnostics(),
        }

    @app.get("/api/news/top-recent")
    def api_news_top_recent(
        limit: int = Query(10, ge=1, le=100),
        min_score: float = Query(0.5, ge=0.0, le=1.0),
    ) -> dict[str, Any]:
        """Highest-scoring recent records — analyst attention triage.

        Mirrors the `catchem top-recent` CLI in HTTP form for shell/script
        callers that prefer JSON over Python. Reads the last 200 records,
        filters by ``min_score``, sorts by score desc, returns top ``limit``.

        Cheap (<5ms typically). Suitable for polling at 30-60s cadence from
        external dashboards.
        """
        sup = _get_supervisor()
        records = sup.storage.recent_records(200, relevant_only=False)
        filtered = [
            r for r in records
            if (r.get("finance_relevance_score") or 0.0) >= min_score
        ]
        filtered.sort(
            key=lambda r: float(r.get("finance_relevance_score") or 0.0),
            reverse=True,
        )
        top = filtered[:limit]
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "limit": limit,
            "min_score": min_score,
            "count": len(top),
            "items": [
                {
                    "capture_id": r.get("capture_id"),
                    "title": r.get("title"),
                    "domain": r.get("domain"),
                    "url": r.get("url"),
                    "score": r.get("finance_relevance_score"),
                    "sentiment": r.get("sentiment_label"),
                    "asset_classes": r.get("asset_classes") or [],
                    "symbols": r.get("candidate_symbols") or [],
                    "published_ts": r.get("published_ts"),
                }
                for r in top
            ],
        }

    @app.get("/api/news/sources")
    def api_news_sources() -> dict[str, Any]:
        """Per-feed source-health snapshot — drives the /sources page.

        Returns one row per configured feed with cumulative polls + success
        counts + items ingested + last error. Stats are in-memory and reset
        on sidecar restart — that's by design; persistent feed-health
        history lives in the structured logs, not this endpoint.

        When the poller is disabled (or hasn't been built yet during
        startup grace), the endpoint returns 200 with ``configured: false``
        and an empty source list so the UI can render a clear "no poller"
        empty state without retrying. Distinguished from the 503 emitted
        by ``/ui/news-poll-now`` because this endpoint is read-only — a
        diagnostic page expecting JSON should always get JSON.
        """
        now_iso = datetime.now(UTC).isoformat()
        poller = _NEWS_POLLER
        if poller is None:
            return {
                "schema_version": 1,
                "generated_at": now_iso,
                "configured": False,
                "total": 0,
                "healthy_count": 0,
                "degraded_count": 0,
                "backed_off_count": 0,
                "sources": [],
            }
        # `feed_health_snapshot()` returns a list of dict copies sorted by
        # feed name. Re-shape so the wire format is stable + analyst-
        # friendly: cumulative polls/successes/failures, computed
        # success_rate in [0, 1], URL + name, last error + status + time.
        sources: list[dict[str, Any]] = []
        for entry in poller.feed_health_snapshot():
            polls = int(entry.get("total_fetches") or 0)
            failures = int(entry.get("total_errors") or 0)
            successes = max(0, polls - failures)
            success_rate = (successes / polls) if polls > 0 else 0.0
            backed_off = bool(entry.get("backed_off"))
            last_status: str
            if backed_off:
                # `backed_off` takes precedence over the underlying
                # ok/error state — the analyst needs to know "we're not
                # even probing this right now" before they go chasing
                # the original error string. The cooldown_until field
                # tells the UI when we'll next probe.
                last_status = "backed_off"
            elif polls == 0:
                last_status = "unknown"
            else:
                last_status = "ok" if entry.get("ok") else "error"
            sources.append({
                "name": entry.get("name") or "",
                "url": entry.get("url") or "",
                "fallback_domain": entry.get("fallback_domain") or "",
                "polls": polls,
                "successes": successes,
                "failures": failures,
                "success_rate": success_rate,
                "items_total": int(entry.get("items_total") or 0),
                "item_count": int(entry.get("item_count") or 0),
                "last_status": last_status,
                "last_status_code": entry.get("status_code"),
                "last_error": entry.get("last_error") if "last_error" in entry else entry.get("error"),
                "last_status_at": entry.get("last_fetch_at"),
                "last_success_at": entry.get("last_success_at"),
                "last_failure_at": entry.get("last_failure_at"),
                "consecutive_errors": int(entry.get("consecutive_errors") or 0),
                "elapsed_ms": entry.get("elapsed_ms"),
                "cooldown_until": entry.get("cooldown_until"),
                "backed_off": backed_off,
            })
        # Sort by url so the table is stable across reloads. (The snapshot
        # itself sorts by feed name; URL is what the user sees in the cell
        # so sorting by it keeps the on-screen order predictable.)
        sources.sort(key=lambda x: x["url"] or "")
        healthy = sum(1 for s in sources if s["last_status"] == "ok")
        degraded = sum(1 for s in sources if s["last_status"] == "error")
        backed_off_count = sum(1 for s in sources if s["last_status"] == "backed_off")
        total_items = sum(s["items_total"] for s in sources)
        return {
            "schema_version": 1,
            "generated_at": now_iso,
            "configured": True,
            "total": len(sources),
            "healthy_count": healthy,
            "degraded_count": degraded,
            "backed_off_count": backed_off_count,
            "total_items": total_items,
            "interval_seconds": poller.interval_seconds,
            "last_run_at": poller.last_run_at.isoformat() if poller.last_run_at else None,
            "sources": sources,
        }

    @app.get("/api/news/awareness")
    def api_news_awareness() -> dict[str, Any]:
        """The live "awareness window" — how fresh + how broad is awareness now.

        Answers the analyst's single question: *given the poll cadence and
        the publisher-side RSS lag, roughly how far back does "now" reach?*
        ``window_estimate_seconds`` ≈ ``poll_interval + median_publisher_lag``:
        the effective span between an event happening and it surfacing here.

        ``sources_by_parser`` tallies the configured feeds by their ``.parser``
        attribute (``rss``, plus any source-pack parsers like ``gdelt`` /
        ``reddit``) so the breadth side of the answer is concrete rather than
        a single "N feeds" number.

        Read-only diagnostic: when the poller is disabled (or not yet built
        during startup grace) this still returns 200 with ``sources_total: 0``
        and null lags / window — the UI renders a degraded panel instead of
        hitting a 503. Mirrors the ``/api/news/sources`` graceful-degrade
        contract.
        """
        now_iso = datetime.now(UTC).isoformat()
        poller = _NEWS_POLLER
        if poller is None:
            return {
                "schema_version": 1,
                "generated_at": now_iso,
                "configured": False,
                "sources_total": 0,
                "sources_by_parser": {},
                "sources_by_category": {},
                "poll_interval_seconds": None,
                "median_publisher_lag_seconds": None,
                "avg_publisher_lag_seconds": None,
                "last_run_at": None,
                "last_new_at": None,
                "total_ingested": 0,
                "dupe_titles_skipped": None,
                "window_estimate_seconds": None,
            }
        # Tally configured feeds by their parser key. FeedSpec.parser defaults
        # to "rss"; source packs contribute "gdelt"/"reddit"/etc. A plain dict
        # (insertion-order) keeps the JSON stable enough for the UI to render
        # without re-sorting.
        sources_by_parser: dict[str, int] = {}
        # Coverage by *domain/topic* (not just parser machinery): each feed is
        # classified from its name prefix / fallback_domain so the operator
        # sees breadth as "watchlist / tickers / regulator / crypto / …"
        # rather than only "rss / gdelt / reddit". Additive to the parser tally.
        sources_by_category: dict[str, int] = {}
        for spec in poller.feeds:
            key = getattr(spec, "parser", "rss") or "rss"
            sources_by_parser[key] = sources_by_parser.get(key, 0) + 1
            category = _classify_feed_category(
                getattr(spec, "name", "") or "",
                key,
                getattr(spec, "fallback_domain", "") or "",
            )
            sources_by_category[category] = sources_by_category.get(category, 0) + 1
        interval = poller.interval_seconds
        median_lag = poller.last_median_publisher_lag_seconds
        avg_lag = poller.last_avg_publisher_lag_seconds
        # The effective window: poll cadence + publisher lag. Only computable
        # when we have a fresh median (no new fresh items this tick → None,
        # matching the poller's own "don't show stale lag" rule).
        window_estimate = (interval + median_lag) if median_lag is not None else None
        return {
            "schema_version": 1,
            "generated_at": now_iso,
            "configured": True,
            "sources_total": len(poller.feeds),
            "sources_by_parser": sources_by_parser,
            "sources_by_category": sources_by_category,
            "poll_interval_seconds": interval,
            "median_publisher_lag_seconds": median_lag,
            "avg_publisher_lag_seconds": avg_lag,
            "last_run_at": poller.last_run_at.isoformat() if poller.last_run_at else None,
            "last_new_at": poller.last_new_at.isoformat() if poller.last_new_at else None,
            "total_ingested": poller.total_ingested,
            # Null-safe passthrough: present only as a number once the poller
            # starts tracking title-level dedupe skips; null otherwise.
            "dupe_titles_skipped": getattr(poller, "last_dupe_titles_skipped", None),
            "window_estimate_seconds": window_estimate,
        }

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
            # User-facing surface: tilde-redacted so /Users/<name>/... does not
            # leak into tooltips, screenshots, or persisted UI state. The
            # archiver retains the resolved absolute path internally; only the
            # JSON projection is redacted.
            "drive_dir": _display_path(_ARCHIVER.drive_dir),
            "interval_seconds": _ARCHIVER.interval_seconds,
            "local_cap_rows": _ARCHIVER.local_cap,
            "last_run_at": _ARCHIVER.last_run_at.isoformat() if _ARCHIVER.last_run_at else None,
            "last_archived_count": _ARCHIVER.last_archived_count,
            "total_archived": _ARCHIVER.total_archived,
            "last_error": _ARCHIVER.last_error,
            "is_archiving": _ARCHIVER.is_archiving,
            "current_csv_path": _display_path(_ARCHIVER.current_csv_path),
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
                            "generated_at": datetime.now(UTC).isoformat(),
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
        # `api/` reserves the whole API surface (incl. /api/docs, /api/redoc,
        # /api/openapi.json, /api/_index) so unknown /api paths 404 cleanly
        # instead of getting served the SPA shell.
        "api/",
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
        html, nonce = _render_spa_with_nonce()
        if html is not None:
            return HTMLResponse(
                html,
                headers={"Content-Security-Policy": _csp_with_nonce(nonce)},
            )
        raise HTTPException(status_code=404, detail="bundle_not_built")

    # The Catchem nav routes /replay → ReplayUploadPage. The existing
    # POST /replay endpoint stays for API consumers, but a bookmarked GET
    # to /replay must serve the SPA shell. Explicit handler avoids the
    # 405-before-fallback case.
    @app.get("/replay", response_class=HTMLResponse, include_in_schema=False)
    def replay_spa() -> HTMLResponse:
        html, nonce = _render_spa_with_nonce()
        if html is not None:
            return HTMLResponse(
                html,
                headers={"Content-Security-Policy": _csp_with_nonce(nonce)},
            )
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
    """Entry point for the ``catchem-api`` console script.

    Bind comes from Settings only. Setting `CATCHEM_API__HOST` /
    `CATCHEM_API__PORT` (nested double-underscore) flows through pydantic.
    The single-underscore forms (`CATCHEM_API_HOST`) are not honored — see
    `tests/test_settings_live_env_override.py::
    test_catchem_api_single_underscore_is_silently_ignored`. Previously
    this function read both via `os.getenv`, which made the single form
    silently override Settings at runtime — the opposite of the contract.
    """
    s = load_settings()
    app = create_app(s)
    uvicorn.run(app, host=s.api.host, port=s.api.port, log_level=s.logging.level.lower())


# Module-level app for `uvicorn catchem.api:app`
app = create_app()
