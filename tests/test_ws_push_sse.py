"""Tests for the SSE transport + Wikimedia firehose in `ws_push.py`.

NO real network. We exercise the SSE machinery with FAKE in-memory byte/text
lines and assert the parse/filter/ingest contract:
  * `_iter_sse_data_lines` — the SSE wire-format splitter: single `data:` line,
    multi-`data:` event, comment/keep-alive heartbeats ignored, and a frame
    split across two reads (partial-buffer carry-over) never parses early.
  * `parse_wikimedia_event` — the recentchange relevance filter: a finance/
    company enwiki title → ParsedItem ("Wikipedia edit: <title>", meta.uri,
    en.wikipedia.org, meta.dt); irrelevant title → None; non-enwiki → None;
    malformed/non-object/missing-title → None.
  * `WebSocketNewsChannel._handle_frame` end-to-end with the `wikimedia` parser
    over fake SSE payload strings: only relevant enwiki events ingest, and the
    same edit URI dedups to a single ingest.
  * `DEFAULT_WS_SOURCES` + the enabled-but-no-sources fallback wiring.
  * `_connect_and_read_sse` drives a stubbed httpx stream (monkeypatched) so the
    SSE read loop is covered without a socket.

The live-network proof (against the real Wikimedia EventStreams endpoint) is a
manual one-shot harness, intentionally NOT a unit test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catchem.ws_push import (
    DEFAULT_WS_SOURCES,
    WebSocketNewsChannel,
    WsSourceSpec,
    _iter_sse_data_lines,
    parse_wikimedia_event,
)

# ── Fakes (mirror test_ws_push.py shapes) ─────────────────────────────────────


class _FakeStorage:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def get_record(self, cap_id: str):
        return self.records.get(cap_id)


class _FakeSupervisor:
    def __init__(self) -> None:
        self.storage = _FakeStorage()
        self.processed: list = []

    def process_capture(self, cap) -> None:
        self.processed.append(cap)
        self.storage.records[cap.capture_id] = {"capture_id": cap.capture_id}


class _StubSettings:
    class paths:
        catchem_output_dir = Path("/tmp/catchem-ws-sse-test")

    class news:
        websocket_enabled = False
        websocket_sources: list[dict[str, str]] = []


def _make_channel(sources=None, settings=None) -> WebSocketNewsChannel:
    return WebSocketNewsChannel(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=settings or _StubSettings(),  # type: ignore[arg-type]
        sources=sources,
    )


def _wiki_event(
    title: str = "Apple Inc.",
    wiki: str = "enwiki",
    uri: str = "https://en.wikipedia.org/wiki/Apple_Inc.",
    dt: str = "2026-05-29T12:34:56Z",
) -> str:
    return json.dumps(
        {
            "title": title,
            "server_name": "en.wikipedia.org",
            "wiki": wiki,
            "type": "edit",
            "meta": {"uri": uri, "dt": dt},
        }
    )


# ── _iter_sse_data_lines: SSE wire-format splitting ───────────────────────────


def test_sse_single_data_line_emits_one_payload() -> None:
    payloads, remainder = _iter_sse_data_lines('data: {"a": 1}\n\n')
    assert payloads == ['{"a": 1}']
    assert remainder == ""


def test_sse_multiple_data_lines_concatenate_into_one_event() -> None:
    # Per spec, multiple data: lines in one block join with "\n".
    buf = "data: line-one\ndata: line-two\n\n"
    payloads, remainder = _iter_sse_data_lines(buf)
    assert payloads == ["line-one\nline-two"]
    assert remainder == ""


def test_sse_ignores_comments_and_non_data_fields() -> None:
    # ":" lines are keep-alive heartbeats; event:/id: are non-data fields.
    buf = ":heartbeat\nevent: message\nid: 42\ndata: payload\n\n"
    payloads, remainder = _iter_sse_data_lines(buf)
    assert payloads == ["payload"]
    assert remainder == ""


def test_sse_partial_frame_is_buffered_until_complete() -> None:
    # First read ends mid-event (no blank-line boundary) → nothing emitted, the
    # whole fragment is carried as remainder.
    payloads, remainder = _iter_sse_data_lines('data: {"partial": ')
    assert payloads == []
    assert remainder == 'data: {"partial": '
    # Second read appends the rest + the boundary → now it parses.
    payloads2, remainder2 = _iter_sse_data_lines(remainder + 'true}\n\n')
    assert payloads2 == ['{"partial": true}']
    assert remainder2 == ""


def test_sse_handles_crlf_and_trailing_partial_after_complete_event() -> None:
    # One complete CRLF event followed by the start of a second (no boundary):
    # the complete one emits, the partial tail is the remainder.
    buf = "data: first\r\n\r\ndata: seco"
    payloads, remainder = _iter_sse_data_lines(buf)
    assert payloads == ["first"]
    assert remainder == "data: seco"


def test_sse_no_boundary_returns_empty_and_keeps_buffer() -> None:
    payloads, remainder = _iter_sse_data_lines("data: x")
    assert payloads == []
    assert remainder == "data: x"


# ── parse_wikimedia_event: relevance filter ───────────────────────────────────


def test_wikimedia_relevant_company_title_parses() -> None:
    item = parse_wikimedia_event(_wiki_event(title="Apple Inc."), "en.wikipedia.org")
    assert item is not None
    assert item.title == "Wikipedia edit: Apple Inc."
    assert item.text == "Wikipedia edit: Apple Inc."
    assert item.url == "https://en.wikipedia.org/wiki/Apple_Inc."
    assert item.domain == "en.wikipedia.org"
    # meta.dt parsed to a tz-aware UTC datetime.
    assert item.published_ts.year == 2026
    assert item.published_ts.tzinfo is not None


def test_wikimedia_relevant_keyword_title_parses() -> None:
    # A finance keyword ("inflation") anywhere in the title is enough.
    item = parse_wikimedia_event(
        _wiki_event(title="2026 inflation in the United States"), "en.wikipedia.org"
    )
    assert item is not None
    assert item.title.startswith("Wikipedia edit: 2026 inflation")


def test_wikimedia_relevant_keyword_is_case_insensitive() -> None:
    item = parse_wikimedia_event(_wiki_event(title="NASDAQ Composite"), "en.wikipedia.org")
    assert item is not None


@pytest.mark.parametrize(
    "title",
    [
        "List of cat breeds",
        "Solar System",
        "1998 FIFA World Cup",
    ],
)
def test_wikimedia_irrelevant_title_skipped(title: str) -> None:
    assert parse_wikimedia_event(_wiki_event(title=title), "en.wikipedia.org") is None


def test_wikimedia_non_enwiki_skipped_even_if_relevant() -> None:
    # Relevant title but wrong wiki (German) → skipped.
    assert (
        parse_wikimedia_event(
            _wiki_event(title="Apple Inc.", wiki="dewiki"), "en.wikipedia.org"
        )
        is None
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[1, 2, 3]",  # JSON list, not an object
        b"\xff\xfe garbage bytes",
        json.dumps({"wiki": "enwiki"}),  # no title
        json.dumps({"wiki": "enwiki", "title": "   "}),  # whitespace title
        json.dumps({"title": "Apple Inc."}),  # no wiki field → not enwiki
        "",
    ],
)
def test_wikimedia_malformed_or_incomplete_skipped(raw) -> None:
    assert parse_wikimedia_event(raw, "en.wikipedia.org") is None


def test_wikimedia_synthesizes_uri_when_meta_uri_missing() -> None:
    raw = json.dumps(
        {"title": "Bitcoin", "wiki": "enwiki", "meta": {"dt": "2026-05-29T00:00:00Z"}}
    )
    item = parse_wikimedia_event(raw, "en.wikipedia.org")
    assert item is not None
    assert item.url == "https://en.wikipedia.org/wiki/Bitcoin"
    assert item.domain == "en.wikipedia.org"


# ── _handle_frame end-to-end with the wikimedia parser ────────────────────────


@pytest.mark.asyncio
async def test_handle_frame_wikimedia_ingests_relevant_skips_irrelevant() -> None:
    spec = WsSourceSpec(
        name="wiki",
        url="https://stream.wikimedia.org/v2/stream/recentchange",
        fallback_domain="en.wikipedia.org",
        kind="sse",
        parser="wikimedia",
    )
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    sup: _FakeSupervisor = chan._sup  # type: ignore[assignment]

    # Relevant enwiki edit → ingests.
    await chan._handle_frame(spec, st, _wiki_event(title="Tesla, Inc.", uri="https://en.wikipedia.org/wiki/Tesla"))
    # Irrelevant title → filtered (counts as parse_failure, no ingest).
    await chan._handle_frame(spec, st, _wiki_event(title="List of birds"))
    # Non-enwiki → filtered.
    await chan._handle_frame(spec, st, _wiki_event(title="Amazon", wiki="frwiki"))
    # Duplicate of the first (same URI) → deduped, no second ingest.
    await chan._handle_frame(spec, st, _wiki_event(title="Tesla, Inc.", uri="https://en.wikipedia.org/wiki/Tesla"))

    assert st.messages_received == 4
    assert st.ingested == 1
    assert len(sup.processed) == 1
    cap = sup.processed[0]
    assert cap.title == "Wikipedia edit: Tesla, Inc."
    assert cap.source_type == "ws"


# ── DEFAULT_WS_SOURCES + enabled-fallback wiring ──────────────────────────────


def test_default_ws_sources_is_the_wikimedia_sse_firehose() -> None:
    assert len(DEFAULT_WS_SOURCES) == 1
    src = DEFAULT_WS_SOURCES[0]
    assert src.kind == "sse"
    assert src.parser == "wikimedia"
    assert "stream.wikimedia.org" in src.url


def test_enabled_with_no_sources_falls_back_to_defaults() -> None:
    class _S:
        class paths:
            catchem_output_dir = Path("/tmp")

        class news:
            websocket_enabled = True
            websocket_sources: list[dict[str, str]] = []

    chan = WebSocketNewsChannel(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=_S(),  # type: ignore[arg-type]
    )
    assert chan.sources == DEFAULT_WS_SOURCES


def test_disabled_with_no_sources_stays_empty() -> None:
    # websocket_enabled False → no fallback (the api.py gate also refuses to
    # construct, but the channel itself must not silently self-enable).
    chan = _make_channel()  # _StubSettings has websocket_enabled = False
    assert chan.sources == ()


def test_explicit_sources_override_defaults() -> None:
    class _S:
        class paths:
            catchem_output_dir = Path("/tmp")

        class news:
            websocket_enabled = True
            websocket_sources = [
                {"name": "my-sse", "url": "https://x/sse", "kind": "sse", "parser": "generic"},
            ]

    chan = WebSocketNewsChannel(
        supervisor=_FakeSupervisor(),  # type: ignore[arg-type]
        settings=_S(),  # type: ignore[arg-type]
    )
    names = [s.name for s in chan.sources]
    assert names == ["my-sse"]
    assert chan.sources[0].kind == "sse"


# ── _connect_and_read_sse over a stubbed httpx stream ─────────────────────────


@pytest.mark.asyncio
async def test_connect_and_read_sse_pumps_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the SSE read loop with a fake httpx.AsyncClient whose stream yields
    canned text chunks (one relevant Wikimedia event split across two chunks)."""
    import catchem.ws_push as ws_push

    spec = WsSourceSpec(
        name="wiki",
        url="https://stream.wikimedia.org/v2/stream/recentchange",
        fallback_domain="en.wikipedia.org",
        kind="sse",
        parser="wikimedia",
    )
    chan = _make_channel(sources=[spec])
    st = chan._states[spec.name]
    sup: _FakeSupervisor = chan._sup  # type: ignore[assignment]

    event = _wiki_event(title="Microsoft", uri="https://en.wikipedia.org/wiki/Microsoft")
    # Split the SSE frame across two chunks to also prove buffer carry-over.
    line = f"data: {event}\n\n"
    chunks = [line[: len(line) // 2], line[len(line) // 2 :]]

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        async def aiter_text(self):
            for c in chunks:
                yield c

    class _FakeStreamCtx:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            assert method == "GET"
            assert headers and headers.get("Accept") == "text/event-stream"
            return _FakeStreamCtx()

    class _FakeHttpx:
        AsyncClient = _FakeClient

        @staticmethod
        def Timeout(**kwargs):
            return None

    monkeypatch.setitem(__import__("sys").modules, "httpx", _FakeHttpx)
    # The method does `import httpx` locally; patching sys.modules covers it.
    await chan._connect_and_read_sse(spec, st)

    assert st.connected is True  # _mark_connected fired
    assert st.messages_received == 1
    assert st.ingested == 1
    assert sup.processed[0].title == "Wikipedia edit: Microsoft"
    _ = ws_push  # keep import referenced
