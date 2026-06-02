"""Unit tests for stream_chat_completion from deepseek.py."""

from __future__ import annotations

from typing import Any
import pytest
import httpx
from catchem.reviewers.deepseek import stream_chat_completion


class MockAsyncResponse:
    def __init__(self, status_code: int, lines: list[str] | None = None, content: str = ""):
        self.status_code = status_code
        self._lines = lines or []
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def aread(self) -> bytes:
        return self._content.encode("utf-8")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class MockAsyncClient:
    def __init__(self, response: MockAsyncResponse):
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_stream_happy_path():
    lines = [
        "data: {\"choices\": [{\"delta\": {\"content\": \"Hello\"}}]}",
        "",  # empty line
        ": heartbeat",  # comment line
        "data: {\"choices\": [{\"delta\": {\"content\": \" World\"}}]}",
        "data: {\"usage\": {\"prompt_tokens\": 10, \"completion_tokens\": 5}}",
        "data: [DONE]",
    ]
    response = MockAsyncResponse(200, lines=lines)
    client = MockAsyncClient(response)

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        client=client,  # type: ignore[arg-type]
    ):
        envelopes.append(env)

    assert envelopes == [
        {"type": "delta", "text": "Hello"},
        {"type": "delta", "text": " World"},
        {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_stream_http_error_non_200():
    response = MockAsyncResponse(400, content="Bad Request Details")
    client = MockAsyncClient(response)

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
        client=client,  # type: ignore[arg-type]
    ):
        envelopes.append(env)

    assert envelopes == [
        {"type": "error", "error": "http_400: Bad Request Details"},
    ]


@pytest.mark.asyncio
async def test_stream_invalid_json_skipped():
    lines = [
        "data: {invalid json}",
        "data: {\"choices\": [{\"delta\": {\"content\": \"Valid\"}}]}",
        "data: [DONE]",
    ]
    response = MockAsyncResponse(200, lines=lines)
    client = MockAsyncClient(response)

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
        client=client,  # type: ignore[arg-type]
    ):
        envelopes.append(env)

    assert envelopes == [
        {"type": "delta", "text": "Valid"},
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_stream_timeout_exception():
    class TimeoutClient:
        def stream(self, *a, **kw):
            raise httpx.TimeoutException("mock timeout")

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
        client=TimeoutClient(),  # type: ignore[arg-type]
    ):
        envelopes.append(env)

    assert len(envelopes) == 1
    assert envelopes[0]["type"] == "error"
    assert "timeout" in envelopes[0]["error"]


@pytest.mark.asyncio
async def test_stream_http_exception():
    class FlakyClient:
        def stream(self, *a, **kw):
            raise httpx.HTTPError("mock transport error")

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
        client=FlakyClient(),  # type: ignore[arg-type]
    ):
        envelopes.append(env)

    assert len(envelopes) == 1
    assert envelopes[0]["type"] == "error"
    assert "transport" in envelopes[0]["error"]


@pytest.mark.asyncio
async def test_stream_own_client_creation(monkeypatch):
    # Mock httpx.AsyncClient initialization
    constructed = False
    closed = False

    class MockAsyncClientReal:
        def __init__(self, *args, **kwargs):
            nonlocal constructed
            constructed = True

        def stream(self, *args, **kwargs):
            return MockAsyncResponse(200, lines=["data: [DONE]"])

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClientReal)

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
    ):
        envelopes.append(env)

    assert constructed
    assert closed
    assert envelopes == [{"type": "done"}]


# ── Synchronous & edge-case unit tests for DeepSeekReviewer ──────────────────

import json
from catchem.taxonomy import default_taxonomy_path, load_taxonomy
from catchem.schemas import AwarenessCaptureView
from catchem.reviewers.deepseek import DeepSeekReviewer, ReviewerError


class _SyncMockResponse:
    def __init__(self, status_code: int, body: dict | str):
        self.status_code = status_code
        if isinstance(body, dict):
            self._body = json.dumps(body)
        else:
            self._body = body
        self.text = self._body[:1000]

    def json(self):
        return json.loads(self._body)


class _SyncMockClient:
    def __init__(self, responses: list[_SyncMockResponse]):
        self._queue = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json or {}))
        if not self._queue:
            raise AssertionError("mock client received unexpected extra call")
        return self._queue.pop(0)

    def close(self):
        pass


@pytest.fixture
def taxonomy():
    return load_taxonomy(default_taxonomy_path())


@pytest.fixture
def cap():
    return AwarenessCaptureView(
        capture_id="cap-ds-sync",
        doc_id="doc-ds-sync",
        title="Fed decision",
        text="The Fed acted.",
        domain="reuters.com",
    )


def test_reviewer_owns_client_close(taxonomy):
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy)
    assert reviewer._owns_client
    reviewer.close()


def test_reviewer_sync_timeout(taxonomy, cap):
    class TimeoutClient:
        def post(self, *args, **kwargs):
            raise httpx.TimeoutException("timeout")

        def close(self):
            pass

    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=TimeoutClient())  # type: ignore
    with pytest.raises(ReviewerError) as exc:
        reviewer.review(cap)
    assert exc.value.code == "timeout"


def test_reviewer_sync_http_error(taxonomy, cap):
    class FlakyClient:
        def post(self, *args, **kwargs):
            raise httpx.HTTPError("error")

        def close(self):
            pass

    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=FlakyClient())  # type: ignore
    with pytest.raises(ReviewerError) as exc:
        reviewer.review(cap)
    assert exc.value.code == "network"


def test_reviewer_sync_404_raises_http_error(taxonomy, cap):
    client = _SyncMockClient([_SyncMockResponse(404, "Not Found")])
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=client)  # type: ignore
    with pytest.raises(ReviewerError) as exc:
        reviewer.review(cap)
    assert exc.value.code == "http_error"


def test_reviewer_sync_non_json_envelope(taxonomy, cap):
    client = _SyncMockClient([_SyncMockResponse(200, "invalid_json")])
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=client)  # type: ignore
    with pytest.raises(ReviewerError) as exc:
        reviewer.review(cap)
    assert exc.value.code == "bad_json"


def test_reviewer_sync_no_choices(taxonomy, cap):
    client = _SyncMockClient([_SyncMockResponse(200, {"choices": []})])
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=client)  # type: ignore
    with pytest.raises(ReviewerError) as exc:
        reviewer.review(cap)
    assert exc.value.code == "bad_json"


def test_reviewer_sync_choices_content_not_object(taxonomy, cap):
    body = {
        "choices": [
            {"message": {"content": "123"}}  # parses as int, not dict
        ]
    }
    client = _SyncMockClient([_SyncMockResponse(200, body)])
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=client)  # type: ignore
    with pytest.raises(ReviewerError) as exc:
        reviewer.review(cap)
    assert exc.value.code == "bad_json"


def test_as_bool_variants():
    from catchem.reviewers.deepseek import _as_bool
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    assert _as_bool(1.5) is True
    assert _as_bool("yes") is True
    assert _as_bool("1") is True
    assert _as_bool("no") is False
    assert _as_bool(None) is False


def test_as_clamped_float_variants():
    from catchem.reviewers.deepseek import _as_clamped_float
    assert _as_clamped_float("invalid") == 0.0
    assert _as_clamped_float(float("nan")) == 0.0
    assert _as_clamped_float(float("inf")) == 0.0
    assert _as_clamped_float(-0.5) == 0.0
    assert _as_clamped_float(1.5) == 1.0


@pytest.mark.asyncio
async def test_stream_additional_branches():
    lines = [
        "invalid_line",
        "data: {\"choices\": [{\"delta\": {\"content\": null}}]}",
        "data: [DONE]",
    ]
    response = MockAsyncResponse(200, lines=lines)
    client = MockAsyncClient(response)

    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
        client=client,  # type: ignore
    ):
        envelopes.append(env)

    assert envelopes == [{"type": "done"}]


@pytest.mark.asyncio
async def test_stream_200_empty_lines():
    # 200 response but empty lines (covers 353->390 loop skip)
    response = MockAsyncResponse(200, lines=[])
    client = MockAsyncClient(response)
    envelopes = []
    async for env in stream_chat_completion(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        messages=[],
        client=client,  # type: ignore
    ):
        envelopes.append(env)
    assert envelopes == []


def test_reviewer_injected_client_close_noop(taxonomy):
    class InjectedClient:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True
    client = InjectedClient()
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy, client=client)  # type: ignore
    assert not reviewer._owns_client
    reviewer.close()
    assert not client.closed


def test_estimate_usd_public(taxonomy):
    reviewer = DeepSeekReviewer(api_key="key", taxonomy=taxonomy)
    assert reviewer.estimate_usd(input_tokens=1000, output_tokens=2000) == pytest.approx(0.00247)


