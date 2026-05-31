"""Streaming SSE endpoint tests for `/api/quant/live-read-stream`.

These pin the over-the-wire contract the QuantScanPage hero depends on:
- Content-type is `text/event-stream`.
- Stream emits a `start` envelope, ≥1 `chunk` envelope, then `done`.
- Local-fallback path (no DeepSeek key) still streams the deterministic
  narrative chunk-by-chunk.
- DeepSeek path streams content from a mocked async iterator.
- The accumulated text matches the local narrative AND/OR the mocked
  DeepSeek payload — never a half-rendered blank.

The DeepSeek transport itself is mocked at the helper level
(`stream_chat_completion`) so the tests never touch the network.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from catchem import api as api_mod
from catchem.api import _word_chunks, create_app
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.settings import load_settings, reload_settings

# ── small parsing helpers ───────────────────────────────────────────────


def _parse_sse_stream(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Walk an SSE byte stream into [(event_name, json_data), ...]."""
    events: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    data_buf: list[str] = []
    for line in raw.splitlines():
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_buf.append(line[len("data:") :].strip())
        elif line == "":
            if name and data_buf:
                joined = "\n".join(data_buf)
                try:
                    payload = json.loads(joined)
                except json.JSONDecodeError:
                    payload = {"_raw": joined}
                events.append((name, payload))
            name = None
            data_buf = []
    # Trailing event without a blank-line terminator (TestClient sometimes
    # cuts the buffer mid-frame). Flush whatever's pending.
    if name and data_buf:
        joined = "\n".join(data_buf)
        try:
            payload = json.loads(joined)
        except json.JSONDecodeError:
            payload = {"_raw": joined}
        events.append((name, payload))
    return events


def _make_signal_record(capture_id: str) -> FinancialImpactRecord:
    now = datetime.now(UTC)
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title="Apple earnings surprise lifts megacap sentiment",
        text_excerpt="Apple earnings surprise lifts megacap sentiment",
        url=f"https://example.com/{capture_id}",
        domain="example.com",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.82,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["AAPL"],
        candidate_entities=["Apple"],
        impact_horizons=["one_day"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.7,
        evidence_sentences=["Apple earnings surprise lifts megacap sentiment"],
        reason_text="equities | earnings",
        component_scores={"asset_class_max": 0.8, "raw_relevance_score": 0.82},
        processing_mode=ProcessingMode.REPLAY_EXISTING,
        model_versions={"zero_shot": "stub/v1"},
        published_ts=now,
        created_at=now,
    )


def _seed_signal_record(capture_id: str) -> None:
    sup = api_mod._SUPERVISOR
    assert sup is not None
    assert sup.storage.insert_record(_make_signal_record(capture_id)) is True


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Same shape as the test_catchem_endpoints fixture."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    # DeepSeek OFF by default even when the developer's .env is keyed; tests
    # that need the hosted path use keyed_client and mock the transport.
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__ENABLED", "false")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__API_KEY", "")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def keyed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Variant fixture with a DeepSeek key wired (still mocked transport)."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__ENABLED", "true")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__API_KEY", "test-key-not-real")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


# ── 1. content-type + envelope shape ────────────────────────────────────


def test_live_read_stream_content_type(client: TestClient) -> None:
    """The endpoint must advertise SSE so EventSource accepts it."""
    with client.stream("GET", "/api/quant/live-read-stream?limit=200") as r:
        assert r.status_code == 200, r.text
        # sse_starlette returns text/event-stream (may include charset).
        ctype = r.headers.get("content-type", "")
        assert "text/event-stream" in ctype, f"unexpected content-type: {ctype!r}"
        # Read the raw body to keep the connection alive long enough for
        # the generator to emit start + chunks + done.
        body = r.read().decode("utf-8")
    events = _parse_sse_stream(body)
    assert any(name == "start" for name, _ in events), f"no start event: {events!r}"
    assert any(name == "done" for name, _ in events), f"no done event: {events!r}"


# ── 2. emits ≥1 chunk + final done (local-fallback path) ────────────────


def test_live_read_stream_emits_chunks_in_local_mode(client: TestClient) -> None:
    """Without a DeepSeek key the endpoint streams the local narrative
    word-by-word so the UI animation still plays."""
    with client.stream("GET", "/api/quant/live-read-stream?limit=200") as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    events = _parse_sse_stream(body)
    names = [n for n, _ in events]
    # Start at index 0, done at the end.
    assert names[0] == "start", names
    assert names[-1] == "done", names
    # At least one chunk between start + done.
    chunks = [data for name, data in events if name == "chunk"]
    assert chunks, f"no chunks emitted: {events!r}"
    # The cumulative text must form a non-empty narrative.
    full = "".join(c.get("text", "") for c in chunks).strip()
    assert len(full) > 10, f"narrative too short: {full!r}"


# ── 3. start envelope carries limit + source ────────────────────────────


def test_live_read_stream_start_envelope_shape(client: TestClient) -> None:
    """The `start` frame is the UI's signal to clear the buffer + show
    the streaming cursor — its shape must be stable."""
    with client.stream("GET", "/api/quant/live-read-stream?limit=500") as r:
        body = r.read().decode("utf-8")
    events = _parse_sse_stream(body)
    start = next(payload for name, payload in events if name == "start")
    assert start["limit"] == 500
    assert start["source"] in {"deepseek", "local"}
    assert "generated_at" in start
    # Done envelope mirrors source + adds ok flag.
    done = next(payload for name, payload in events if name == "done")
    assert done["ok"] is True
    assert done["source"] in {"deepseek", "local"}


# ── 4. Empty context never spends external reviewer calls ────────────────


def test_live_read_json_skips_deepseek_when_context_empty(
    keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed reviewer must still stay local when there is no signal to summarize."""
    sup = api_mod._SUPERVISOR
    assert sup is not None
    client = sup.reviewers.deepseek()
    assert client is not None

    def fail_post(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("DeepSeek should not be called for empty context")

    monkeypatch.setattr(client._client, "post", fail_post)

    r = keyed_client.get("/api/quant/live-read?limit=200")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "local"
    assert body["fallback_reason"] == "empty_context"
    assert body["context"]["window_records"] == 0


def test_live_read_stream_skips_deepseek_when_context_empty(
    keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSE hero path must not open DeepSeek streaming for a zero-row snapshot."""

    async def fail_stream(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        raise AssertionError("DeepSeek stream should not be called for empty context")
        yield {}

    monkeypatch.setattr(
        "catchem.reviewers.deepseek.stream_chat_completion",
        fail_stream,
    )

    with keyed_client.stream("GET", "/api/quant/live-read-stream?limit=200") as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    events = _parse_sse_stream(body)
    start = next(p for n, p in events if n == "start")
    done = next(p for n, p in events if n == "done")
    assert start["source"] == "local"
    assert done["source"] == "local"
    assert done["fallback_reason"] == "empty_context"


# ── 5. DeepSeek path uses mocked streaming helper ────────────────────────


def test_live_read_stream_deepseek_path_uses_streaming_helper(
    keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the key is set, the endpoint must consume `stream_chat_completion`
    envelopes and forward `delta` frames as `chunk` events."""

    _seed_signal_record("stream-deepseek-signal")
    fake_chunks = [
        "**Dominant story:** ",
        "Tech rotation underway. ",
        "Risk concentrated in mega-caps.",
    ]

    async def fake_stream(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for txt in fake_chunks:
            yield {"type": "delta", "text": txt}
        yield {
            "type": "usage",
            "usage": {"prompt_tokens": 120, "completion_tokens": 60},
        }
        yield {"type": "done"}

    monkeypatch.setattr(
        "catchem.reviewers.deepseek.stream_chat_completion",
        fake_stream,
    )

    with keyed_client.stream("GET", "/api/quant/live-read-stream?limit=200") as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    events = _parse_sse_stream(body)
    chunks = [p.get("text", "") for n, p in events if n == "chunk"]
    full = "".join(chunks)
    # All three mocked deltas must be present in order.
    assert "Dominant story" in full
    assert "Tech rotation underway" in full
    assert "Risk concentrated in mega-caps" in full
    # The done envelope reports source=deepseek + usd_cost (non-negative).
    done = next(p for n, p in events if n == "done")
    assert done["source"] == "deepseek"
    assert done.get("usd_cost", 0) >= 0


# ── 6. DeepSeek error -> falls back to local narrative on the wire ───────


def test_live_read_stream_deepseek_error_falls_back(
    keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the streaming helper yields an error envelope, the endpoint
    must finish the stream with the local narrative AND a `fallback_reason`
    on the `done` event so the UI can surface a warning chip."""

    _seed_signal_record("stream-deepseek-error-signal")

    async def fake_stream_error(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "error", "error": "http_503: upstream offline"}

    monkeypatch.setattr(
        "catchem.reviewers.deepseek.stream_chat_completion",
        fake_stream_error,
    )

    with keyed_client.stream("GET", "/api/quant/live-read-stream?limit=200") as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    events = _parse_sse_stream(body)
    done = next(p for n, p in events if n == "done")
    assert done["source"] == "local"
    assert "fallback_reason" in done
    # And we must have streamed SOME text via chunk events (the local
    # fallback narrative), not just emitted start+done.
    chunks = [p.get("text", "") for n, p in events if n == "chunk"]
    assert chunks, "fallback path should still emit chunks"
    assert "".join(chunks).strip(), "fallback text is empty"


# ── 7. unit test for the word-chunk helper ──────────────────────────────


def test_word_chunks_preserves_text_round_trip() -> None:
    """The helper must split into non-empty chunks whose concatenation
    equals the input (modulo whitespace normalization)."""
    text = "The Fed raised rates by 25 bps. Apple fell 2%."
    chunks = _word_chunks(text, group_size=2)
    assert chunks, "empty chunk list"
    joined = "".join(chunks)
    # Whitespace collapsed but tokens preserved + ordered.
    assert re.sub(r"\s+", " ", joined).strip() == text


def test_word_chunks_handles_empty_string() -> None:
    """Edge case — defensive, since the local narrative could be blank
    in pathological windows."""
    assert _word_chunks("") == []
    assert _word_chunks("   ") == []
