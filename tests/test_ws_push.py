"""Tests for the real-time WebSocket PUSH channel (`ws_push.py`).

NO real network or sockets. We exercise:
  * `parse_ws_message` — JSON frame → ParsedItem (title/url/domain), envelope
    unwrap, URL-less squawk synthesis, and the unusable-frame → None paths.
  * `WebSocketNewsChannel._handle_frame` — the parse → dedup → ingest pipeline
    with a fake in-memory supervisor: ingest happens once per unique URL.
  * The reconnect/backoff loop — a fake source that "drops" (raises) then a
    stop() proves the backoff ladder is consulted and cancellation is clean.
  * disabled / empty-config = no-op (no tasks spawned, stats degrade).
  * A TestClient hit on `GET /api/news/ws-status` for the enabled:false path.

The fake WS is a stub async iterator yielding sample JSON frames, swapped in
by monkeypatching the channel's `_connect_and_read` so no `websockets`/`httpx_ws`
import is ever needed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.settings import load_settings, reload_settings
from catchem.ws_push import (
    WS_BACKOFF_LADDER_SECONDS,
    WebSocketNewsChannel,
    WsSourceSpec,
    parse_ws_message,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeStorage:
    """Records inserted captures keyed by capture_id; get_record honors them."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def get_record(self, cap_id: str):
        return self.records.get(cap_id)


class _FakeSupervisor:
    """Minimal supervisor: a storage with get_record + a process_capture sink.

    `process_capture` mimics the real upsert — it materializes a row keyed by
    the capture's deterministic id, so a second ingest of the same content is a
    no-op at the storage layer exactly like the real pipeline.
    """

    def __init__(self) -> None:
        self.storage = _FakeStorage()
        self.processed: list = []

    def process_capture(self, cap) -> None:
        self.processed.append(cap)
        self.storage.records[cap.capture_id] = {"capture_id": cap.capture_id}


class _StubSettings:
    """Settings stub exposing only what the channel reads."""

    class paths:
        catchem_output_dir = Path("/tmp/catchem-ws-test")

    class news:
        websocket_sources: list[dict[str, str]] = []


def _make_channel(sources=None, settings=None) -> WebSocketNewsChannel:
    return WebSocketNewsChannel(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=settings or _StubSettings(),  # type: ignore[arg-type]
        sources=sources,
    )


# ── parse_ws_message ─────────────────────────────────────────────────────────


def test_parse_ws_message_basic_json_frame() -> None:
    item = parse_ws_message(
        '{"title": "Fed cuts rates 25bps", "url": "https://wire.example.com/a"}',
        fallback_domain="wire.example.com",
    )
    assert item is not None
    assert item.title == "Fed cuts rates 25bps"
    assert item.url == "https://wire.example.com/a"
    assert item.domain == "wire.example.com"
    # published_ts is stamped at parse time (frames rarely carry one).
    assert isinstance(item.published_ts, datetime)


def test_parse_ws_message_alternate_keys_and_html_strip() -> None:
    # headline/link aliases + an HTML body that must be stripped.
    item = parse_ws_message(
        '{"headline": "Acme beats", "link": "https://x.example.com/acme",'
        ' "body": "<p>Acme <b>beat</b> estimates.</p>"}',
        fallback_domain="x.example.com",
    )
    assert item is not None
    assert item.title == "Acme beats"
    assert item.text == "Acme beat estimates."
    assert "<b>" not in item.text


def test_parse_ws_message_unwraps_data_envelope() -> None:
    item = parse_ws_message(
        '{"type": "news", "data": {"title": "Wrapped", "url": "https://e.example.com/x"}}',
        fallback_domain="e.example.com",
    )
    assert item is not None
    assert item.title == "Wrapped"
    assert item.url == "https://e.example.com/x"


def test_parse_ws_message_synthesizes_url_for_squawk_without_link() -> None:
    # URL-less squawk frame: a deterministic pseudo-URL is synthesized from the
    # fallback domain + headline so the capture_id stays unique + stable.
    item = parse_ws_message(
        '{"text": "BREAKING: oil spikes"}', fallback_domain="squawk.example.com"
    )
    assert item is not None
    assert item.domain == "squawk.example.com"
    assert item.url.startswith("https://squawk.example.com/ws/")
    # Same headline → same synthesized URL (idempotent dedup key).
    again = parse_ws_message(
        '{"text": "BREAKING: oil spikes"}', fallback_domain="squawk.example.com"
    )
    assert again is not None
    assert again.url == item.url


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[1, 2, 3]",            # JSON list, not an object
        '{"foo": "bar"}',        # object but no title/body
        '{"title": "   "}',      # whitespace-only title, no body
        "",
    ],
)
def test_parse_ws_message_returns_none_on_unusable_frames(raw: str) -> None:
    assert parse_ws_message(raw, fallback_domain="x.com") is None


# ── _handle_frame: parse → dedup → ingest ─────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_frame_ingests_once_per_unique_url() -> None:
    spec = WsSourceSpec("wire", "wss://wire.example.com/ws", "wire.example.com")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    sup: _FakeSupervisor = chan._sup  # type: ignore[assignment]

    frame_a = '{"title": "Story A", "url": "https://wire.example.com/a"}'
    frame_b = '{"title": "Story B", "url": "https://wire.example.com/b"}'

    await chan._handle_frame(spec, st, frame_a)
    await chan._handle_frame(spec, st, frame_a)  # exact dup → skipped (LRU)
    # www. + tracking param variant of A → canonicalizes to the same key.
    await chan._handle_frame(
        spec, st, '{"title": "Story A", "url": "https://www.wire.example.com/a?utm_source=ws"}'
    )
    await chan._handle_frame(spec, st, frame_b)

    # Two unique URLs ⇒ exactly two ingests, despite four frames.
    assert len(sup.processed) == 2
    assert st.messages_received == 4
    assert st.ingested == 2
    # Source type is "ws" so downstream can tell push from poll.
    assert all(cap.source_type == "ws" for cap in sup.processed)


@pytest.mark.asyncio
async def test_handle_frame_respects_storage_guard() -> None:
    """A capture_id already in storage (e.g. the poller ingested the same URL)
    is skipped here — cross-channel idempotency backstop."""
    spec = WsSourceSpec("wire", "wss://wire.example.com/ws", "wire.example.com")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    sup: _FakeSupervisor = chan._sup  # type: ignore[assignment]

    from catchem.demo import _deterministic_capture_id

    # Pre-seed storage with the exact capture_id this frame will derive.
    text, url = "Story A", "https://wire.example.com/a"
    # The channel uses item.text (== title when no body) for the id.
    pre_id = _deterministic_capture_id(text, url)
    sup.storage.records[pre_id] = {"capture_id": pre_id}

    await chan._handle_frame(spec, st, f'{{"title": "{text}", "url": "{url}"}}')
    # Storage already had it → no new process_capture.
    assert sup.processed == []
    assert st.ingested == 0


@pytest.mark.asyncio
async def test_handle_frame_counts_parse_failures_without_raising() -> None:
    spec = WsSourceSpec("wire", "wss://wire.example.com/ws", "")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    await chan._handle_frame(spec, st, "garbage{{")
    assert st.parse_failures == 1
    assert st.ingested == 0
    assert st.messages_received == 1


# ── reconnect / backoff ───────────────────────────────────────────────────────


def test_backoff_ladder_climbs_and_caps() -> None:
    chan = _make_channel(sources=[WsSourceSpec("s", "wss://s/ws")])
    # 1 failure → first rung; climbs; caps at the last rung.
    assert chan._backoff_seconds(1) == WS_BACKOFF_LADDER_SECONDS[0]
    assert chan._backoff_seconds(2) == WS_BACKOFF_LADDER_SECONDS[1]
    assert chan._backoff_seconds(999) == WS_BACKOFF_LADDER_SECONDS[-1]
    # Zero / negative clamps to the first rung (defensive).
    assert chan._backoff_seconds(0) == WS_BACKOFF_LADDER_SECONDS[0]


@pytest.mark.asyncio
async def test_run_source_invokes_backoff_on_drop_then_cancels_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a connection drop: `_connect_and_read` raises, the loop must
    bump consecutive_failures, consult the backoff ladder, and exit cleanly
    when stop() fires — all without real sockets."""
    spec = WsSourceSpec("flaky", "wss://flaky.example.com/ws", "flaky.example.com")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]

    attempts = {"n": 0}

    async def _boom(_spec, _st) -> None:
        attempts["n"] += 1
        raise ConnectionError("simulated drop")

    backoff_calls: list[int] = []
    real_backoff = chan._backoff_seconds

    def _spy_backoff(consecutive: int) -> float:
        backoff_calls.append(consecutive)
        # Shrink the real delay so the test doesn't actually wait seconds.
        real_backoff(consecutive)
        return 0.01

    monkeypatch.setattr(chan, "_connect_and_read", _boom)
    monkeypatch.setattr(chan, "_backoff_seconds", _spy_backoff)

    chan._stop.clear()
    task = asyncio.create_task(chan._run_source(spec))
    # Let it churn through a few failed connect→backoff cycles.
    await asyncio.sleep(0.08)
    chan._stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert attempts["n"] >= 2, "reconnect loop should retry after a drop"
    assert backoff_calls, "backoff ladder must be consulted on each drop"
    assert st.consecutive_failures >= 2
    assert st.reconnects >= 2
    assert st.state == "stopped"  # clean exit on stop()


@pytest.mark.asyncio
async def test_clean_connect_resets_failure_count_and_marks_connected() -> None:
    spec = WsSourceSpec("s", "wss://s/ws", "s.local")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    st.consecutive_failures = 4
    chan._mark_connected(st)
    assert st.connected is True
    assert st.consecutive_failures == 0
    assert st.state == "connected"


# ── disabled / empty config = no-op ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_sources_start_is_noop() -> None:
    chan = _make_channel(sources=[])
    chan.start()  # no running loop needed — guarded before get_running_loop()
    assert chan._tasks == []
    stats = chan.stats()
    assert stats["sources_total"] == 0
    assert stats["running"] is False
    await chan.stop()  # idempotent, no tasks


@pytest.mark.asyncio
async def test_start_degrades_when_no_ws_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """When neither `websockets` nor `httpx_ws` import, the channel stays idle:
    no tasks spawned, every source flagged disabled. Graceful degradation."""
    import catchem.ws_push as ws_push

    monkeypatch.setattr(ws_push, "_ws_lib_available", lambda: None)
    chan = _make_channel(sources=[WsSourceSpec("s", "wss://s/ws")])
    chan.start()
    assert chan._tasks == []
    assert chan._lib is None
    st = chan._states["s"]
    assert st.state == "disabled"
    assert st.last_error == "no_ws_library"


def test_channel_builds_sources_from_settings() -> None:
    class _S:
        class paths:
            catchem_output_dir = Path("/tmp")

        class news:
            websocket_sources = [
                {"name": "wire", "url": "wss://a/ws", "fallback_domain": "a.com"},
                {"url": "wss://b/ws"},          # name defaults to url
                {"name": "no-url"},              # dropped (no url)
            ]

    chan = WebSocketNewsChannel(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=_S(),  # type: ignore[arg-type]
    )
    names = [s.name for s in chan.sources]
    assert names == ["wire", "wss://b/ws"]
    assert chan.sources[0].fallback_domain == "a.com"


# ── /api/news/ws-status endpoint ──────────────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with the WS channel disabled (default). The status endpoint
    must answer 200 + {enabled:false} so the UI renders a dormant panel."""
    from catchem.api import create_app

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_NEWS__WEBSOCKET_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_ws_status_disabled_returns_enabled_false(client: TestClient) -> None:
    r = client.get("/api/news/ws-status")
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False}


def test_ws_status_enabled_returns_stats_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a constructed (not started) channel and assert the full stats
    envelope shape the UI reads."""
    from catchem import api as api_module

    chan = _make_channel(
        sources=[WsSourceSpec("wire", "wss://wire.example.com/ws", "wire.example.com")]
    )
    monkeypatch.setattr(api_module, "_WS_CHANNEL", chan, raising=False)

    r = client.get("/api/news/ws-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["schema_version"] == 1
    assert body["sources_total"] == 1
    assert body["connected_count"] == 0
    assert body["messages_received"] == 0
    assert body["ingested"] == 0
    assert isinstance(body["sources"], list) and len(body["sources"]) == 1
    assert body["sources"][0]["name"] == "wire"
    assert body["sources"][0]["state"] == "idle"


# ──────────────────────────────────────────────────────────────────────────────
# Graceful-close handling + parser containment (v79 reliability fixes)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graceful_close_with_frames_resets_failure_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graceful server close that DELIVERED frames is a healthy session, not a
    failure: consecutive_failures resets to 0 so the reconnect uses the fastest
    backoff rung instead of being penalized like a flaky drop."""
    spec = WsSourceSpec("healthy", "wss://h/ws", "h.local")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    st.consecutive_failures = 5  # pretend we'd accrued failures earlier

    async def _graceful_with_frames(_spec, _st) -> None:
        _st.messages_received += 3  # session delivered frames…
        await asyncio.sleep(0)      # …then the server closed (graceful return)

    backoff_calls: list[int] = []

    def _spy_backoff(consecutive: int) -> float:
        backoff_calls.append(consecutive)
        return 0.01  # shrink the wait so the test doesn't sleep seconds

    monkeypatch.setattr(chan, "_connect_and_read", _graceful_with_frames)
    monkeypatch.setattr(chan, "_backoff_seconds", _spy_backoff)

    chan._stop.clear()
    task = asyncio.create_task(chan._run_source(spec))
    await asyncio.sleep(0.06)
    chan._stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert st.reconnects >= 1
    assert st.consecutive_failures == 0, "a productive graceful close is not a failure"
    assert backoff_calls and all(c == 0 for c in backoff_calls), (
        "reconnect after a healthy session must use the fastest backoff rung"
    )
    assert st.state == "stopped"


@pytest.mark.asyncio
async def test_graceful_close_without_frames_counts_as_soft_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graceful close that delivered ZERO frames (server accepts then closes
    empty) is treated as a soft failure so backoff throttles the otherwise-hot
    reconnect loop."""
    spec = WsSourceSpec("empty", "wss://e/ws", "e.local")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]

    async def _graceful_no_frames(_spec, _st) -> None:
        await asyncio.sleep(0)  # return immediately, no messages delivered

    backoff_calls: list[int] = []

    def _spy_backoff(consecutive: int) -> float:
        backoff_calls.append(consecutive)
        return 0.01

    monkeypatch.setattr(chan, "_connect_and_read", _graceful_no_frames)
    monkeypatch.setattr(chan, "_backoff_seconds", _spy_backoff)

    chan._stop.clear()
    task = asyncio.create_task(chan._run_source(spec))
    await asyncio.sleep(0.06)
    chan._stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert st.consecutive_failures >= 2, "empty graceful closes must accrue soft failures"
    assert st.reconnects >= 2
    assert max(backoff_calls) >= 2, "backoff must climb when empty closes repeat"
    assert st.state == "stopped"


@pytest.mark.asyncio
async def test_handle_frame_survives_throwing_custom_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom parser that RAISES must be contained inside _handle_frame
    (count a parse failure + skip), never bubble out — otherwise _run_source
    misreads it as a connection drop and tears down a healthy socket."""
    import catchem.ws_push as ws_push

    def _throwing_parser(_raw, _domain):
        raise ValueError("malformed frame the custom parser choked on")

    monkeypatch.setitem(ws_push._FRAME_PARSERS, "boom", _throwing_parser)
    spec = WsSourceSpec("custom", "wss://c/ws", "c.local", parser="boom")
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    sup: _FakeSupervisor = chan._sup  # type: ignore[assignment]

    # Must NOT raise even though the parser throws.
    await chan._handle_frame(spec, st, '{"anything": "here"}')

    assert st.parse_failures == 1
    assert st.messages_received == 1
    assert st.ingested == 0
    assert sup.processed == []
