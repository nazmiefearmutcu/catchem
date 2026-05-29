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
from .news_poller import (
    _USER_AGENT,
    ParsedItem,
    _canonical_url,
    _parse_ts,
    _resolve_domain,
    _SeenCache,
    _strip_html,
)
from .settings import Settings
from .supervisor import Supervisor

logger = get_logger("catchem.ws_push")

# Reconnect backoff ladder (seconds). Climbed on each consecutive failed
# connect/read; reset to the first rung on a clean connect. Capped at the top
# rung so a permanently-dead source is probed ~once a minute rather than going
# silent. Mirrors the poller's circuit-breaker philosophy (BACKOFF_LADDER) but
# for a persistent socket rather than a per-tick fetch.
WS_BACKOFF_LADDER_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)

# Hard ceiling on the SSE accumulation buffer. The SSE read timeout is
# intentionally infinite (an idle stream between events is normal), so a
# misbehaving / hostile endpoint that streams bytes without ever emitting an
# event boundary ("\n\n") would otherwise grow `buffer` without limit until
# OOM. A few MB is far larger than any legitimate single SSE event; crossing it
# means the peer is not speaking SSE, so we drop the connection and let the
# backoff loop reconnect.
MAX_SSE_BUFFER_BYTES = 4 * 1024 * 1024

# Common JSON keys a squawk/news frame uses for its title + URL. Frames vary
# wildly across providers, so we probe a small ordered set rather than pin one
# shape. First non-empty wins. Operators with an exotic frame can pre-transform
# upstream; this covers the de-facto conventions (headline/title/text, link/url).
_TITLE_KEYS: tuple[str, ...] = ("title", "headline", "summary", "text", "body", "message")
_URL_KEYS: tuple[str, ...] = ("url", "link", "href", "uri", "permalink", "source_url")

# ── Wikimedia EventStreams (SSE) relevance filter ──────────────────────────────
# Wikimedia EventStreams (https://stream.wikimedia.org/v2/stream/recentchange) is
# a genuine public, auth-free SSE firehose of every wiki edit in real time. The
# raw volume is enormous (hundreds of edits/sec across all wikis), so we keep it
# sane by ingesting ONLY enwiki edits whose page title brushes a curated
# finance/company keyword set. A finance/company Wikipedia page being edited is a
# weak-but-real awareness signal — it cannot be a hard dependency, hence OFF by
# default — but it proves the SSE path end-to-end against a real public stream.
#
# Substring keywords matched case-insensitively against the page title. Kept
# deliberately tight; broadening this trades signal for noise.
_WIKI_FINANCE_KEYWORDS: frozenset[str] = frozenset(
    {
        "stock",
        "market",
        "nasdaq",
        "earnings",
        "merger",
        "bank",
        "inflation",
        "federal reserve",
        "bitcoin",
        "crypto",
    }
)
# Mega-cap company names (lowercase). Matched as case-insensitive substrings so
# e.g. "Apple Inc." or "Apple (company)" both hit "apple". Short/ambiguous tokens
# are intentionally omitted to avoid false positives on unrelated pages.
_WIKI_COMPANY_NAMES: frozenset[str] = frozenset(
    {
        "apple inc",
        "microsoft",
        "nvidia",
        "amazon",
        "alphabet",
        "google",
        "meta platforms",
        "tesla",
        "berkshire hathaway",
        "jpmorgan",
        "visa inc",
        "mastercard",
        "exxon",
        "walmart",
        "saudi aramco",
        "taiwan semiconductor",
    }
)


@dataclass(frozen=True)
class WsSourceSpec:
    """One configured push source — a WebSocket (default) or an SSE stream.

    `fallback_domain` attributes frames that carry no resolvable URL host
    (some squawk feeds ship a bare headline). Mirrors FeedSpec.fallback_domain.

    `kind` selects the transport: ``"ws"`` (long-lived WebSocket, the default)
    or ``"sse"`` (Server-Sent Events over an httpx streaming GET). Both share
    the exact same dedup + ingest path and reconnect/backoff loop downstream.

    `parser` names the per-source frame parser. ``"generic"`` (default) runs the
    tolerant squawk/news-frame probe (`parse_ws_message`); ``"wikimedia"`` runs
    the recentchange relevance filter (`parse_wikimedia_event`). This lets a
    single SSE/WS reader serve heterogeneous firehoses without branching upstream.
    """

    name: str
    url: str
    fallback_domain: str = ""
    kind: str = "ws"  # "ws" | "sse"
    parser: str = "generic"  # "generic" | "wikimedia"


# Documented default push sources. Available out of the box but NOT used unless
# the operator flips `settings.news.websocket_enabled` AND leaves
# `settings.news.websocket_sources` empty (then this list fills in). Today this
# is exactly one entry: the Wikimedia EventStreams SSE firehose, the one genuine
# public auth-free real-time stream we can prove live. Keep additions here
# strictly free/no-auth/stable — no API keys, no fragile vendor endpoints.
DEFAULT_WS_SOURCES: tuple[WsSourceSpec, ...] = (
    WsSourceSpec(
        name="wikimedia-recentchange",
        url="https://stream.wikimedia.org/v2/stream/recentchange",
        fallback_domain="en.wikipedia.org",
        kind="sse",
        parser="wikimedia",
    ),
)


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


def _wikimedia_title_is_relevant(title: str) -> bool:
    """True iff a Wikipedia page title brushes the finance/company filter.

    Case-insensitive substring test against the curated keyword set + the
    mega-cap company-name set. This is the volume governor for the Wikimedia
    firehose — without it every wiki edit on earth would ingest.
    """
    low = title.casefold()
    if any(kw in low for kw in _WIKI_FINANCE_KEYWORDS):
        return True
    return any(name in low for name in _WIKI_COMPANY_NAMES)


def parse_wikimedia_event(raw: str | bytes, fallback_domain: str = "") -> ParsedItem | None:
    """Parse one Wikimedia EventStreams `recentchange` event into a ParsedItem.

    The event shape is ``{"title", "server_name", "wiki", "type",
    "meta": {"uri", "dt"}, ...}``. We ingest ONLY when:
      * the event is valid JSON object with a non-empty string ``title``,
      * ``wiki == "enwiki"`` (English Wikipedia — keeps language sane), and
      * the title passes the finance/company relevance filter.
    Anything else (malformed, non-enwiki, irrelevant title) returns None so the
    caller counts it a parse-skip and never ingests. Title → ``"Wikipedia edit:
    <title>"``, url → ``meta.uri``, domain → ``en.wikipedia.org``, published_ts
    → ``meta.dt`` (best-effort ISO parse, falls back to now()).
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("wiki") != "enwiki":
        return None
    title_raw = data.get("title")
    if not isinstance(title_raw, str) or not title_raw.strip():
        return None
    title = title_raw.strip()
    if not _wikimedia_title_is_relevant(title):
        return None

    meta = data.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    uri = meta.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        # No canonical edit URI → synthesize a stable page URL so the dedup
        # key is content-derived and the row still links somewhere sensible.
        slug = title.replace(" ", "_")
        uri = f"https://en.wikipedia.org/wiki/{slug}"
    uri = uri.strip()
    dt_raw = meta.get("dt") if isinstance(meta.get("dt"), str) else None

    domain = _resolve_domain(uri, fallback_domain) or fallback_domain or "en.wikipedia.org"
    return ParsedItem(
        title=f"Wikipedia edit: {title}",
        text=f"Wikipedia edit: {title}",
        url=uri,
        domain=domain,
        published_ts=_parse_ts(dt_raw),
    )


# Frame-parser registry: maps a source's `parser` key to the callable that
# turns one raw frame/event into a ParsedItem (or None to skip). Both have the
# identical (raw, fallback_domain) signature so `_handle_frame` stays generic.
_FRAME_PARSERS: dict[str, Any] = {
    "generic": parse_ws_message,
    "wikimedia": parse_wikimedia_event,
}


def _iter_sse_data_lines(buffer: str) -> tuple[list[str], str]:
    """Split an accumulated SSE text buffer into complete `data:` payloads.

    SSE events are blocks of newline-delimited fields terminated by a blank
    line (``\\n\\n``); a single logical event may carry multiple ``data:`` lines
    which the spec concatenates with ``\\n``. We split the buffer on the event
    boundary (``\\n\\n``), keeping the trailing partial block — everything after
    the last boundary — as the verbatim ``remainder`` to prepend to the next
    read, so an event is never parsed until fully received. For each *complete*
    block we collect its ``data:`` field values (ignoring ``:`` comment/keep-
    alive heartbeats and non-data fields like ``event:``/``id:``) and emit one
    payload per block that had at least one data line. Returns ``(payloads,
    remainder)``.
    """
    # Normalize CRLF so the boundary test is purely "\n\n".
    normalized = buffer.replace("\r\n", "\n").replace("\r", "\n")
    if "\n\n" not in normalized:
        return [], normalized  # no complete event yet — keep buffering
    *blocks, remainder = normalized.split("\n\n")
    payloads: list[str] = []
    for block in blocks:
        data_lines = [
            line[len("data:") :].lstrip(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if data_lines:
            payloads.append("\n".join(data_lines))
    return payloads, remainder


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
            # Fallback: enabled but no sources configured → use the documented
            # DEFAULT_WS_SOURCES (the Wikimedia SSE firehose) so the channel has
            # something real to prove live without forcing the operator to hand-
            # author a source dict. Only kicks in when the operator opted in.
            if not self._sources and self._websocket_enabled(settings):
                self._sources = DEFAULT_WS_SOURCES
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
    def _websocket_enabled(settings: Settings) -> bool:
        """Defensive read of `settings.news.websocket_enabled` (default False)."""
        return bool(getattr(getattr(settings, "news", None), "websocket_enabled", False))

    @staticmethod
    def _sources_from_settings(settings: Settings) -> tuple[WsSourceSpec, ...]:
        """Build specs from `settings.news.websocket_sources` (list of dicts).

        Each dict is `{name, url, fallback_domain, kind?, parser?}`; entries
        without a `url` are dropped. `kind` defaults to ``"ws"`` and `parser`
        to ``"generic"`` so existing WS configs are unaffected. Defensive
        getattr keeps stub-settings callers (tests) working even when the
        `news` namespace lacks the field.
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
            kind = str(entry.get("kind") or "ws").strip().lower() or "ws"
            parser = str(entry.get("parser") or "generic").strip().lower() or "generic"
            out.append(
                WsSourceSpec(
                    name=str(entry.get("name") or url),
                    url=url,
                    fallback_domain=str(entry.get("fallback_domain") or ""),
                    kind=kind,
                    parser=parser,
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

        Transport-aware degradation: SSE sources ride httpx (always present, a
        hard dep) so they always spawn. WS sources need an importable WS client
        lib (`websockets` / `httpx_ws`); when none is available every *WS-kind*
        source is marked ``disabled`` and skipped, while SSE sources still run.
        Logs once when WS sources are dark so the operator can see why.
        """
        if self._tasks:
            return  # already running
        if not self._sources:
            logger.info("ws_push_no_sources")
            return
        self._lib = _ws_lib_available()
        ws_specs = [s for s in self._sources if s.kind != "sse"]
        if self._lib is None and ws_specs:
            for spec in ws_specs:
                st = self._states[spec.name]
                st.state = "disabled"
                st.last_error = "no_ws_library"
            logger.warning(
                "ws_push_degraded_no_library",
                ws_sources=len(ws_specs),
                hint="pip install websockets (or httpx_ws) to enable the WS firehose",
            )
        self._stop.clear()
        loop = asyncio.get_running_loop()
        self.started_at = datetime.now(UTC)
        spawned = 0
        for spec in self._sources:
            # Skip WS-kind sources when no WS lib is available; SSE always runs.
            if spec.kind != "sse" and self._lib is None:
                continue
            task = loop.create_task(
                self._run_source(spec), name=f"catchem-ws-{spec.name}"
            )
            self._tasks.append(task)
            spawned += 1
        logger.info("ws_push_started", spawned=spawned, sources=len(self._sources), library=self._lib)

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
            messages_before = st.messages_received
            try:
                await self._connect_and_read(spec, st)
                # _connect_and_read returns only on a graceful server-side
                # close. That is NOT a transport failure, so a session that
                # delivered ≥1 frame resets the failure count and reconnects at
                # the fastest cadence. Only a graceful close that produced ZERO
                # frames is treated as a soft failure — that's the signature of
                # a server accepting then immediately closing, and counting it
                # lets backoff throttle the otherwise-hot reconnect loop.
                if self._stop.is_set():
                    break
                st.connected = False
                st.reconnects += 1
                if st.messages_received > messages_before:
                    st.consecutive_failures = 0
                else:
                    st.consecutive_failures += 1
                st.state = "reconnecting"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st.connected = False
                # A drop via exception (abnormal WS close, SSE network error) is
                # the COMMON real-world failure. Mirror the graceful-return path:
                # a session that delivered ≥1 frame is evidence the source is
                # alive, so reset the failure count and reconnect at the fastest
                # cadence — only escalate backoff when ZERO frames were seen
                # (the signature of a never-connecting / dead endpoint).
                if st.messages_received > messages_before:
                    st.consecutive_failures = 0
                else:
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
        """Open the transport (WS or SSE) and pump frames.

        Returns on a graceful server-side close; raises on any connection or
        read error (the caller's loop converts that into a backoff + retry).
        Each successful connect resets `consecutive_failures` and flips the
        source to ``connected`` so `stats()` reflects a live firehose.
        """
        if spec.kind == "sse":
            await self._connect_and_read_sse(spec, st)
        elif self._lib == "websockets":
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

    async def _connect_and_read_sse(self, spec: WsSourceSpec, st: _SourceState) -> None:
        """Open an SSE stream (httpx streaming GET) and pump `data:` events.

        Server-Sent Events ride a plain long-lived HTTP response with
        ``Accept: text/event-stream``; the server keeps the body open and emits
        newline-delimited ``data: <json>`` frames (blank line = event boundary).
        We stream raw bytes, accumulate a text buffer, and hand each complete
        ``data:`` payload to `_handle_frame`. httpx is a hard dependency, so this
        path needs no optional WS lib. Returns on graceful close; raises on any
        network/transport error so the caller's backoff loop reconnects.
        """
        import httpx

        # Long read timeout: an idle SSE stream is normal between events, so we
        # must NOT treat read-silence as an error. connect/write/pool keep finite
        # budgets so a dead endpoint still fails fast into backoff.
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        # A descriptive User-Agent is REQUIRED by some public SSE firehoses —
        # notably Wikimedia EventStreams, which 403s a missing/generic UA per its
        # robot policy. Reuse the poller's UA so both channels are attributable.
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": _USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", spec.url, headers=headers) as resp:
                resp.raise_for_status()
                self._mark_connected(st)
                buffer = ""
                async for chunk in resp.aiter_text():
                    if self._stop.is_set():
                        return
                    buffer += chunk
                    payloads, buffer = _iter_sse_data_lines(buffer)
                    # Guard against an endpoint that never emits an event
                    # boundary: the remainder buffer would grow unbounded under
                    # the infinite read timeout. Raise so the caller's backoff
                    # loop reconnects instead of leaking memory until OOM.
                    if len(buffer) > MAX_SSE_BUFFER_BYTES:
                        raise RuntimeError(
                            f"sse_buffer_overflow: {len(buffer)} bytes "
                            f"without an event boundary (max {MAX_SSE_BUFFER_BYTES})"
                        )
                    for payload in payloads:
                        if self._stop.is_set():
                            return
                        await self._handle_frame(spec, st, payload)

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
        parser = _FRAME_PARSERS.get(spec.parser, parse_ws_message)
        try:
            item = parser(raw, spec.fallback_domain)
        except Exception as exc:
            # A custom parser registered via `_FRAME_PARSERS` must not be able
            # to tear down the reader loop — an uncaught raise here would bubble
            # out of `_connect_and_read` and `_run_source` would misread it as a
            # connection FAILURE (bumping backoff + dropping a healthy socket).
            # Built-in parsers are defensive; third-party ones may not be. Treat
            # a throwing parser exactly like an unparseable frame: count + skip.
            st.parse_failures += 1
            logger.info("ws_push_parse_failed", source=spec.name, error=str(exc))
            return
        if item is None:
            # For the Wikimedia firehose most events are intentionally filtered
            # out (wrong wiki / irrelevant title) — that's a skip, not a failure,
            # but we count it the same so stats stay simple. messages_received
            # still reflects true volume seen on the wire.
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
    "DEFAULT_WS_SOURCES",
    "WS_BACKOFF_LADDER_SECONDS",
    "WebSocketNewsChannel",
    "WsSourceSpec",
    "parse_wikimedia_event",
    "parse_ws_message",
]
