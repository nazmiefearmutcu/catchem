"""Real-time WebSocket PUSH channel — complements the HTTP poller.

The HTTP poller (`news_poller.py`) pulls RSS/JSON on a fixed 10s cadence. That
is fine for publisher RSS (which itself lags minutes), but it leaves money on
the table for genuine PUSH firehoses — squawk feeds, news-wire WebSockets,
exchange tickers — where the source streams a frame the instant something
happens. This module opens a long-lived WebSocket to each configured source so
those frames arrive with near-zero latency instead of waiting for the next poll.

Design notes (mirrors the poller's safety posture):
  * OFF by default. `settings.news.websocket_enabled` is False and
    `websocket_sources` is empty, so the channel constructs but connects to
    nothing unless the operator opts in. No fragile hardcoded endpoints.
  * NO hard dependency. The WebSocket client lib (`websockets`, or
    `httpx_ws`) is imported lazily inside a try/except. If neither importable,
    the channel logs once and stays idle — the rest of the sidecar is
    unaffected. This keeps `import catchem` cheap and the wheel slim.
  * Same ingest path as the poller. Each parsed frame goes through
    `build_capture → write_jsonl → supervisor.process_capture`, the exact
    `news_poller._ingest_one` shape, so a WS arrival is indistinguishable from
    a polled one downstream (same dedup, same storage-guard, same JSONL
    replay copy).
  * Dedup parity. Canonical-URL LRU (`_SeenCache`) + deterministic capture_id
    (`_deterministic_capture_id`) reused verbatim from the poller, so an
    article that arrives on BOTH the WS firehose and a polled RSS feed is
    ingested exactly once regardless of which lands first... within the
    process. (Cross-channel dedup beyond the LRU is still backstopped by the
    storage PRIMARY KEY, same as the poller.)
  * Reconnect with exponential backoff on any drop; clean cancel on stop().
  * Defensive everywhere: a malformed frame, a dead source, or a parse error
    never tears down the channel or another source's task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .demo import _deterministic_capture_id, build_capture, write_jsonl
from .logging import get_logger
from .news_poller import ParsedItem, _canonical_url, _resolve_domain, _SeenCache, _strip_html
from .settings import Settings
from .supervisor import Supervisor

logger = get_logger("catchem.ws_push")

# Reconnect backoff ladder (seconds). Climbed on each consecutive failed
# connect/read; reset to the first rung on a clean connect. Capped at the top
# rung so a permanently-dead source is probed ~once a minute rather than going
# silent. Mirrors the poller's circuit-breaker philosophy (BACKOFF_LADDER) but
# for a persistent socket rather than a per-tick fetch.
WS_BACKOFF_LADDER_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)

# Common JSON keys a squawk/news frame uses for its title + URL. Frames vary
# wildly across providers, so we probe a small ordered set rather than pin one
# shape. First non-empty wins. Operators with an exotic frame can pre-transform
# upstream; this covers the de-facto conventions (headline/title/text, link/url).
_TITLE_KEYS: tuple[str, ...] = ("title", "headline", "summary", "text", "body", "message")
_URL_KEYS: tuple[str, ...] = ("url", "link", "href", "uri", "permalink", "source_url")


@dataclass(frozen=True)
class WsSourceSpec:
    """One configured WebSocket source.

    `fallback_domain` attributes frames that carry no resolvable URL host
    (some squawk feeds ship a bare headline). Mirrors FeedSpec.fallback_domain.
    """

    name: str
    url: str
    fallback_domain: str = ""


@dataclass
class _SourceState:
    """Per-source mutable runtime state, surfaced via `WebSocketNewsChannel.stats`."""

    name: str
    url: str
    state: str = "idle"  # idle | connecting | connected | reconnecting | error | stopped | disabled
    connected: bool = False
    messages_received: int = 0
    ingested: int = 0
    parse_failures: int = 0
    reconnects: int = 0
    consecutive_failures: int = 0
    last_message_at: datetime | None = None
    last_error: str | None = None


def _ws_lib_available() -> str | None:
    """Return the name of an importable WS client lib, or None.

    Probes lazily (import only happens here, never at module import) so the
    channel adds NO hard dependency. Order of preference: `websockets` (pure,
    widely installed) then `httpx_ws` (rides the existing httpx dep). Returns
    the module key the connect path understands, or None to degrade gracefully.
    """
    try:
        import websockets  # noqa: F401

        return "websockets"
    except Exception:
        pass
    try:
        import httpx_ws  # noqa: F401

        return "httpx_ws"
    except Exception:
        pass
    return None


def parse_ws_message(raw: str | bytes, fallback_domain: str = "") -> ParsedItem | None:
    """Parse one WS frame (JSON) into a ParsedItem, or None if unusable.

    Tolerant by design — a frame that isn't JSON, isn't an object, or lacks a
    usable title is dropped (None), never raised. The title/url probing reuses
    the same HTML-stripping + domain-resolution helpers the RSS parser uses so
    a WS arrival normalizes identically to a polled one.

    A frame with a title but no URL still parses: we synthesize a stable
    pseudo-URL from the fallback domain + the title so the deterministic
    capture_id stays unique per distinct headline (squawk feeds are often
    URL-less). A frame with neither title nor body is dropped.
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        # A bare list / scalar frame has no headline+url pair we can key on.
        return None

    # Unwrap a single-level {"data": {...}} / {"payload": {...}} envelope —
    # common in socket.io-style and pub/sub firehoses.
    for env_key in ("data", "payload", "message"):
        inner = data.get(env_key)
        if isinstance(inner, dict) and any(k in inner for k in (*_TITLE_KEYS, *_URL_KEYS)):
            data = inner
            break

    title_raw = ""
    for k in _TITLE_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            title_raw = v.strip()
            break
    url = ""
    for k in _URL_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            url = v.strip()
            break

    # Body text: prefer a dedicated body/summary distinct from the title, else
    # reuse the title (matches the RSS parser's text-falls-back-to-title rule).
    body_raw = ""
    for k in ("body", "text", "summary", "description", "content"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            body_raw = v.strip()
            break
    title = _strip_html(title_raw) or _strip_html(body_raw)
    text = _strip_html(body_raw) or title
    if not title and not text:
        return None
    if not title:
        title = text[:120].rstrip()

    if not url:
        # URL-less squawk frame: synthesize a deterministic pseudo-URL so the
        # capture_id is stable + unique per distinct headline. Not a real link,
        # but the UI shows the domain and the dedup key is content-derived.
        host = fallback_domain or "ws.local"
        slug = "".join(c if c.isalnum() else "-" for c in title.lower())[:80].strip("-")
        url = f"https://{host}/ws/{slug or 'item'}"

    domain = _resolve_domain(url, fallback_domain)
    return ParsedItem(
        title=title,
        text=text,
        url=url,
        domain=domain or fallback_domain or "ws.local",
        published_ts=datetime.now(UTC),
    )


class WebSocketNewsChannel:
    """Manages a fleet of long-lived WebSocket source tasks.

    One asyncio task per source; each connects, reads frames, parses + dedups +
    ingests, and reconnects with exponential backoff on drop. `start()` spawns
    the tasks (no-op when disabled / no sources / no WS lib); `stop()` cancels
    them cleanly. `stats()` returns a JSON-able snapshot for the status endpoint.
    """

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        settings: Settings,
        sources: Iterable[WsSourceSpec] | None = None,
    ) -> None:
        self._sup = supervisor
        self._settings = settings
        if sources is not None:
            self._sources: tuple[WsSourceSpec, ...] = tuple(sources)
        else:
            self._sources = self._sources_from_settings(settings)
        # Shared canonical-URL LRU — parity with the poller's `_seen`. A URL
        # that already arrived (poller or WS) within the LRU window is skipped
        # before the storage round-trip.
        self._seen = _SeenCache()
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._lib: str | None = None  # resolved at start()
        self._states: dict[str, _SourceState] = {
            s.name: _SourceState(name=s.name, url=s.url) for s in self._sources
        }
        self.started_at: datetime | None = None

    # ── construction helpers ────────────────────────────────────────────────
    @staticmethod
    def _sources_from_settings(settings: Settings) -> tuple[WsSourceSpec, ...]:
        """Build specs from `settings.news.websocket_sources` (list of dicts).

        Each dict is `{name, url, fallback_domain}`; entries without a `url`
        are dropped. Defensive getattr keeps stub-settings callers (tests)
        working even when the `news` namespace lacks the field.
        """
        raw = getattr(getattr(settings, "news", None), "websocket_sources", None) or []
        out: list[WsSourceSpec] = []
        for entry in raw:
            try:
                url = str(entry.get("url") or "").strip()
            except AttributeError:
                continue
            if not url:
                continue
            out.append(
                WsSourceSpec(
                    name=str(entry.get("name") or url),
                    url=url,
                    fallback_domain=str(entry.get("fallback_domain") or ""),
                )
            )
        return tuple(out)

    # ── public accessors ────────────────────────────────────────────────────
    @property
    def sources(self) -> tuple[WsSourceSpec, ...]:
        return self._sources

    def stats(self) -> dict[str, Any]:
        """JSON-able snapshot for `GET /api/news/ws-status`."""
        per_source = [
            {
                "name": st.name,
                "url": st.url,
                "state": st.state,
                "connected": st.connected,
                "messages_received": st.messages_received,
                "ingested": st.ingested,
                "parse_failures": st.parse_failures,
                "reconnects": st.reconnects,
                "consecutive_failures": st.consecutive_failures,
                "last_message_at": st.last_message_at.isoformat() if st.last_message_at else None,
                "last_error": st.last_error,
            }
            for st in (self._states[s.name] for s in self._sources)
        ]
        connected = sum(1 for st in self._states.values() if st.connected)
        messages = sum(st.messages_received for st in self._states.values())
        ingested = sum(st.ingested for st in self._states.values())
        last_msgs = [st.last_message_at for st in self._states.values() if st.last_message_at]
        last_message_at = max(last_msgs).isoformat() if last_msgs else None
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "enabled": True,
            "library": self._lib,
            "running": bool(self._tasks),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "sources_total": len(self._sources),
            "connected_count": connected,
            "messages_received": messages,
            "ingested": ingested,
            "last_message_at": last_message_at,
            "sources": per_source,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Spawn one reader task per source. No-op when nothing to do.

        Degrades gracefully when no WS client lib is importable: every source
        is marked ``disabled`` and no task is spawned. Logs once so the
        operator can see why the firehose is dark.
        """
        if self._tasks:
            return  # already running
        if not self._sources:
            logger.info("ws_push_no_sources")
            return
        self._lib = _ws_lib_available()
        if self._lib is None:
            for st in self._states.values():
                st.state = "disabled"
                st.last_error = "no_ws_library"
            logger.warning(
                "ws_push_degraded_no_library",
                sources=len(self._sources),
                hint="pip install websockets (or httpx_ws) to enable the WS firehose",
            )
            return
        self._stop.clear()
        loop = asyncio.get_running_loop()
        self.started_at = datetime.now(UTC)
        for spec in self._sources:
            task = loop.create_task(
                self._run_source(spec), name=f"catchem-ws-{spec.name}"
            )
            self._tasks.append(task)
        logger.info("ws_push_started", sources=len(self._sources), library=self._lib)

    async def stop(self) -> None:
        """Cancel every source task and await their teardown."""
        if not self._tasks:
            return
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        for st in self._states.values():
            st.state = "stopped"
            st.connected = False
        logger.info("ws_push_stopped")

    # ── per-source loop ────────────────────────────────────────────────────
    def _backoff_seconds(self, consecutive_failures: int) -> float:
        """Reconnect delay for the given consecutive-failure count (capped)."""
        idx = min(max(0, consecutive_failures - 1), len(WS_BACKOFF_LADDER_SECONDS) - 1)
        return WS_BACKOFF_LADDER_SECONDS[idx]

    async def _run_source(self, spec: WsSourceSpec) -> None:
        """Long-lived connect→read→ingest loop for one source, with backoff.

        Each connection attempt is wrapped so any error (connect refused, read
        drop, protocol error) increments the failure count, schedules a
        backoff sleep, and retries — until `stop()` sets the stop event. A
        clean connect resets the failure count so a flaky source that recovers
        snaps back to the fastest reconnect cadence.
        """
        st = self._states[spec.name]
        while not self._stop.is_set():
            st.state = "connecting"
            try:
                await self._connect_and_read(spec, st)
                # _connect_and_read only returns on a graceful close (server
                # closed the socket) — treat as a drop and reconnect.
                if self._stop.is_set():
                    break
                st.connected = False
                st.consecutive_failures += 1
                st.reconnects += 1
                st.state = "reconnecting"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st.connected = False
                st.consecutive_failures += 1
                st.reconnects += 1
                st.state = "error"
                st.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.info(
                    "ws_push_source_error",
                    source=spec.name,
                    error=str(exc),
                    consecutive_failures=st.consecutive_failures,
                )
            if self._stop.is_set():
                break
            delay = self._backoff_seconds(st.consecutive_failures)
            # Sleep for the backoff, but wake immediately on stop().
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
        st.connected = False
        st.state = "stopped"

    async def _connect_and_read(self, spec: WsSourceSpec, st: _SourceState) -> None:
        """Open the socket (via whichever lib resolved) and pump frames.

        Returns on a graceful server-side close; raises on any connection or
        read error (the caller's loop converts that into a backoff + retry).
        Each successful connect resets `consecutive_failures` and flips the
        source to ``connected`` so `stats()` reflects a live firehose.
        """
        if self._lib == "websockets":
            import websockets

            async with websockets.connect(spec.url) as ws:
                self._mark_connected(st)
                async for message in ws:
                    if self._stop.is_set():
                        return
                    await self._handle_frame(spec, st, message)
        elif self._lib == "httpx_ws":
            import httpx
            import httpx_ws

            async with httpx.AsyncClient() as client:
                async with httpx_ws.aconnect_ws(spec.url, client) as ws:
                    self._mark_connected(st)
                    while not self._stop.is_set():
                        message = await ws.receive_text()
                        await self._handle_frame(spec, st, message)
        else:  # pragma: no cover - start() guards this
            raise RuntimeError("no_ws_library")

    def _mark_connected(self, st: _SourceState) -> None:
        st.connected = True
        st.consecutive_failures = 0
        st.state = "connected"
        st.last_error = None
        logger.info("ws_push_connected", source=st.name, url=st.url)

    async def _handle_frame(self, spec: WsSourceSpec, st: _SourceState, raw: str | bytes) -> None:
        """Parse → dedup → ingest one frame. Never raises (errors are counted).

        Mirrors the poller's per-item filter: canonical-URL LRU first (cheap),
        then the deterministic capture_id storage guard, then ingest on a
        worker thread (`process_capture` holds the SQLite write lock briefly,
        so we keep it off the event loop). All counters live on `st` for stats.
        """
        st.messages_received += 1
        st.last_message_at = datetime.now(UTC)
        item = parse_ws_message(raw, spec.fallback_domain)
        if item is None:
            st.parse_failures += 1
            return
        canon = _canonical_url(item.url)
        if canon in self._seen:
            return
        self._seen.add(canon)
        cap_id = _deterministic_capture_id(item.text, item.url)
        try:
            if self._sup.storage.get_record(cap_id) is not None:
                return
        except Exception as exc:  # storage hiccup must not kill the reader
            logger.info("ws_push_storage_check_failed", source=spec.name, error=str(exc))
            return
        try:
            await asyncio.to_thread(self._ingest_one, item)
            st.ingested += 1
        except Exception as exc:
            logger.info(
                "ws_push_ingest_failed", source=spec.name, url=item.url, error=str(exc)
            )

    def _ingest_one(self, item: ParsedItem) -> None:
        """Process one item through the shared supervisor — `news_poller` parity.

        Identical shape to `NewsPoller._ingest_one`: build the capture, write a
        best-effort JSONL replay copy under ``live-news/`` (failure here never
        blocks ingest), then process through the warm supervisor. The
        deterministic capture_id keeps `insert_record` upsert-safe.
        """
        cap = build_capture(
            title=item.title,
            text=item.text,
            domain=item.domain,
            url=item.url,
            published_ts=item.published_ts,
            source_type="ws",
        )
        try:
            archive_root = self._settings.paths.catchem_output_dir / "live-news"
            archive_root.mkdir(parents=True, exist_ok=True)
            write_jsonl(cap, archive_root)
        except OSError as exc:
            logger.info("ws_push_archive_failed", url=item.url, error=str(exc))
        self._sup.process_capture(cap)


__all__ = [
    "WS_BACKOFF_LADDER_SECONDS",
    "WebSocketNewsChannel",
    "WsSourceSpec",
    "parse_ws_message",
]
