"""Regression tests for bug-hunt file group F-net.

Covers five confirmed findings:
  1. rate_limit.TokenBucket.allow  — negative `elapsed` on a brand-new key
     under-credited the bucket and denied the first request.
  2. webhook.is_safe_webhook_url   — integer / hex / short-form loopback URLs
     bypassed the SSRF gate.
  3. static_assets.get_static_path — leaked an ExitStack callback (and, on
     zipped installs, a temp file) on every call.
  4. ws_push._run_source           — exception-drop path never reset
     consecutive_failures after a frame-delivering session.
  5. ws_push._connect_and_read_sse — unbounded SSE accumulation buffer.

Each test FAILS against the pre-fix source and PASSES after.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from catchem import ws_push
from catchem.rate_limit import TokenBucket
from catchem.webhook import is_safe_webhook_url
from catchem.ws_push import WebSocketNewsChannel, WsSourceSpec


# ── Finding 1: rate-limit first-request denial ────────────────────────────────
def test_new_key_starts_at_full_capacity_capacity_one():
    """A capacity=1 bucket MUST allow the very first request from a fresh key.

    Pre-fix: the defaultdict factory sampled monotonic() AFTER `now`, so
    `elapsed` was negative and the bucket dropped below 1 token → (False, ...).
    """
    bucket = TokenBucket(capacity=1, rate_per_sec=1 / 60)
    allowed, retry_after = bucket.allow("fresh-client")
    assert allowed is True, f"first request wrongly denied (retry_after={retry_after})"
    assert retry_after == 0.0


def test_new_key_starts_at_full_capacity_general():
    """A fresh key has exactly `capacity` tokens — it can burst the full cap."""
    bucket = TokenBucket(capacity=5, rate_per_sec=5 / 60)
    for i in range(5):
        allowed, _ = bucket.allow("burst-client")
        assert allowed is True, f"request {i} of an empty-history burst denied"
    # The 6th immediately exceeds the (just-drained) bucket.
    allowed, retry_after = bucket.allow("burst-client")
    assert allowed is False
    assert retry_after > 0.0


# ── Finding 2: SSRF loopback shorthand bypass ─────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/services/x",  # integer form of 127.0.0.1
        "http://127.1/services/x",        # short dotted form
        "http://0x7f.0.0.1/services/x",   # hex-octet form
        "http://0x7f000001/services/x",   # single hex word
    ],
)
def test_loopback_shorthands_rejected(url):
    """Legacy IPv4 encodings of 127.0.0.1 MUST be rejected by the SSRF gate."""
    assert is_safe_webhook_url(url) is False, f"{url} bypassed the SSRF filter"


@pytest.mark.parametrize(
    "url",
    [
        "https://slack.com/services/x",
        "https://hooks.foo.io/abc",
        "https://example.com/path",
    ],
)
def test_real_public_hostnames_still_pass(url):
    """Genuine alphabetic DNS names must keep passing (no false positives)."""
    assert is_safe_webhook_url(url) is True


def test_canonical_loopback_and_metadata_still_rejected():
    """Dotted-quad loopback / metadata stay rejected (no regression)."""
    assert is_safe_webhook_url("http://127.0.0.1/x") is False
    assert is_safe_webhook_url("http://169.254.169.254/latest/meta") is False
    assert is_safe_webhook_url("http://10.0.0.5/x") is False


# ── Finding 3: static-asset ExitStack memoization ─────────────────────────────
def test_get_static_path_does_not_leak_exitstack_callbacks(monkeypatch):
    """Repeated get_static_path() for the same name enters the ExitStack ONCE.

    Pre-fix: every call ran `_KEEPALIVE.enter_context(as_file(...))`, so the
    keepalive ExitStack's callback list (and, on zip installs, a temp file)
    grew by one per call. We count enter_context invocations across many calls.
    """
    from catchem import static_assets

    # Reset memo so this test is order-independent.
    static_assets._RESOLVED_PATHS.clear()

    calls = {"n": 0}
    real_enter = static_assets._KEEPALIVE.enter_context

    def _counting_enter(cm):
        calls["n"] += 1
        return real_enter(cm)

    monkeypatch.setattr(static_assets._KEEPALIVE, "enter_context", _counting_enter)
    # Make sure the env override is off so we exercise the packaged-resource path.
    monkeypatch.delenv("CATCHEM_STATIC_DIR", raising=False)

    name = "app/index.html"
    first = static_assets.get_static_path(name)
    if first is None:
        pytest.skip("packaged static asset app/index.html not present in this install")

    for _ in range(25):
        again = static_assets.get_static_path(name)
        assert again == first
    assert calls["n"] == 1, (
        f"enter_context ran {calls['n']} times for one asset across 26 calls "
        "— the ExitStack leak is back"
    )


# ── Finding 4: backoff reset on exception drop ────────────────────────────────
def _make_channel(tmp_settings) -> WebSocketNewsChannel:
    sup = types.SimpleNamespace(process_capture=lambda *a, **k: None)
    spec = WsSourceSpec(name="src", url="wss://example.test/stream", kind="ws")
    return WebSocketNewsChannel(supervisor=sup, settings=tmp_settings, sources=[spec])


def test_exception_drop_resets_failures_after_frames(tmp_settings):
    """A session that delivered frames then dropped via exception resets backoff.

    Pre-fix: the `except Exception` branch unconditionally did
    `consecutive_failures += 1`, so a healthy firehose that produced thousands
    of frames before a transient blip was throttled like a dead endpoint.
    """
    ch = _make_channel(tmp_settings)
    st = ch._states["src"]
    st.consecutive_failures = 4  # pretend prior penalty accrued

    async def _run():
        async def fake_connect(spec, state):
            state.messages_received += 1
            raise RuntimeError("simulated abnormal close")

        ch._connect_and_read = fake_connect  # type: ignore[assignment]

        def stop_after_one(n):
            ch._stop.set()
            return 0.0

        ch._backoff_seconds = stop_after_one  # type: ignore[assignment]
        await ch._run_source(ch.sources[0])

    asyncio.run(_run())
    assert st.messages_received >= 1
    assert st.consecutive_failures == 0, (
        f"frame-delivering session left consecutive_failures="
        f"{st.consecutive_failures} (should reset to 0)"
    )


def test_exception_drop_increments_failures_when_no_frames(tmp_settings):
    """A drop that produced ZERO frames still escalates backoff (dead endpoint)."""
    ch = _make_channel(tmp_settings)
    st = ch._states["src"]
    st.consecutive_failures = 2

    async def _run():
        async def fake_connect(spec, state):
            raise RuntimeError("connect refused")  # no frames delivered

        ch._connect_and_read = fake_connect  # type: ignore[assignment]

        def stop_after_one(n):
            ch._stop.set()
            return 0.0

        ch._backoff_seconds = stop_after_one  # type: ignore[assignment]
        await ch._run_source(ch.sources[0])

    asyncio.run(_run())
    assert st.messages_received == 0
    assert st.consecutive_failures == 3, (
        f"zero-frame drop should escalate backoff, got {st.consecutive_failures}"
    )


# ── Finding 5: SSE buffer cap ─────────────────────────────────────────────────
class _FakeStreamResponse:
    """Minimal async-streaming httpx.Response stand-in."""

    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        return None

    async def aiter_text(self):
        for c in self._chunks:
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, chunks, **kwargs):
        self._chunks = chunks

    def stream(self, method, url, headers=None):
        return _FakeStreamResponse(self._chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_sse_buffer_overflow_raises_instead_of_growing(tmp_settings, monkeypatch):
    """An SSE endpoint that never emits an event boundary MUST be dropped.

    Pre-fix: `buffer` grew unboundedly under the infinite read timeout. We feed
    chunks summing to > MAX_SSE_BUFFER_BYTES with NO '\\n\\n' and assert the
    reader raises (so the backoff loop reconnects) rather than buffering forever.
    """
    import httpx

    over = ws_push.MAX_SSE_BUFFER_BYTES
    # Two chunks, no event boundary, total clearly over the cap.
    chunks = ["x" * (over // 2 + 1), "y" * (over // 2 + 1)]

    def _fake_client_ctor(*args, **kwargs):
        return _FakeAsyncClient(chunks, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_ctor)

    ch = _make_channel(tmp_settings)
    spec = WsSourceSpec(name="sse", url="https://evil.test/stream", kind="sse")
    st = ws_push._SourceState(name="sse", url=spec.url)

    async def _run():
        await ch._connect_and_read_sse(spec, st)

    with pytest.raises(RuntimeError, match="sse_buffer_overflow"):
        asyncio.run(_run())


def test_sse_normal_event_under_cap_is_parsed(tmp_settings, monkeypatch):
    """A well-formed SSE event under the cap is delivered, not dropped."""
    import httpx

    handled: list[Any] = []
    chunks = ['data: {"hello": "world"}\n\n']

    def _fake_client_ctor(*args, **kwargs):
        return _FakeAsyncClient(chunks, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_ctor)

    ch = _make_channel(tmp_settings)
    spec = WsSourceSpec(name="sse", url="https://ok.test/stream", kind="sse")
    st = ws_push._SourceState(name="sse", url=spec.url)

    async def fake_handle(spec_, st_, payload):
        handled.append(payload)

    ch._handle_frame = fake_handle  # type: ignore[assignment]

    async def _run():
        await ch._connect_and_read_sse(spec, st)

    asyncio.run(_run())
    assert handled == ['{"hello": "world"}']
