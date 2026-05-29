"""DeepSeek API reviewer.

Calls the DeepSeek chat completions endpoint with JSON-mode output, parses
the response against the catchem taxonomy, and returns a `ReviewPayload`.

Privacy note: this is the ONLY catchem code path that contacts an external
service. The Settings → reviewers toggle gates it; disabled state restores
the "no cloud services are contacted" guarantee.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..logging import get_logger
from ..schemas import AwarenessCaptureView
from ..taxonomy import Taxonomy
from .base import (
    REVIEWER_DEEPSEEK,
    ReviewerError,
    ReviewPayload,
)
from .prompts import SYSTEM_INSTRUCTION, build_user_prompt

logger = get_logger("catchem.reviewers.deepseek")

# Default endpoint — overridable via Settings for self-hosted gateways.
DEFAULT_BASE_URL = "https://api.deepseek.com"

# Pricing as of mid-2025 (USD per 1M tokens). Off-peak discount NOT applied
# because we don't know when the call fires. Cache-hit pricing also NOT
# applied — every catchem article body is fresh content, never cached.
PRICING_PER_1M = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
}

# Sentinel set on the connection-level timeout. 30s is plenty for the
# article-classification prompt; off-peak DeepSeek calls land in 1-3s.
_HTTPX_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class DeepSeekReviewer:
    """Implements the `Reviewer` protocol against api.deepseek.com."""

    reviewer_id = REVIEWER_DEEPSEEK

    def __init__(
        self,
        *,
        api_key: str,
        taxonomy: Taxonomy,
        model: str = "deepseek-chat",
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.1,
        max_output_tokens: int = 600,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            # We treat empty key as a configuration error rather than
            # silently sending Authorization: Bearer "" — DeepSeek replies
            # 401 in that case, but the error is clearer here.
            raise ReviewerError("auth", "missing DeepSeek API key")
        self._api_key = api_key
        self._taxonomy = taxonomy
        self.model = model
        self._base_url = base_url.rstrip("/")
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        # Allow tests to inject a mock httpx.Client without rewriting
        # the constructor signature.
        self._client = client or httpx.Client(timeout=_HTTPX_TIMEOUT)
        self._owns_client = client is None

    @property
    def reviewer_version(self) -> str:
        # The model string itself carries a strong "version" signal; we
        # combine it with the prompt-shape version so future prompt
        # changes are detectable on the compare page.
        return f"{self.model}|prompt-v1"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ── core call ────────────────────────────────────────────────────────
    def review(self, cap: AwarenessCaptureView) -> ReviewPayload:
        t0 = time.perf_counter()
        body_text = cap.text or ""
        user_prompt = build_user_prompt(
            taxonomy=self._taxonomy,
            title=cap.title,
            body=body_text,
            domain=cap.domain,
            url=cap.url or cap.canonical_url,
        )
        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=request_body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise ReviewerError("timeout", f"DeepSeek timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ReviewerError("network", f"transport error: {exc}") from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if response.status_code == 401 or response.status_code == 403:
            raise ReviewerError("auth", f"DeepSeek auth failed: {response.text[:200]}")
        if response.status_code == 429:
            raise ReviewerError("rate_limit", f"DeepSeek rate-limit: {response.text[:200]}")
        if response.status_code >= 500:
            raise ReviewerError("upstream", f"DeepSeek 5xx: {response.status_code}")
        if response.status_code != 200:
            raise ReviewerError(
                "http_error",
                f"DeepSeek HTTP {response.status_code}: {response.text[:200]}",
            )

        try:
            envelope = response.json()
        except json.JSONDecodeError as exc:
            raise ReviewerError("bad_json", f"DeepSeek non-JSON response: {exc}") from exc

        choices = envelope.get("choices") or []
        if not choices:
            raise ReviewerError("bad_json", "DeepSeek returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ReviewerError(
                "bad_json",
                f"DeepSeek content not parseable as JSON: {exc}; head={content[:120]!r}",
            ) from exc
        if not isinstance(parsed, dict):
            raise ReviewerError(
                "bad_json", f"DeepSeek content is not an object (got {type(parsed).__name__})"
            )

        usage = envelope.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        usd_cost = self._estimate_usd(input_tokens=input_tokens, output_tokens=output_tokens)

        return _payload_from_parsed(
            parsed=parsed,
            cap=cap,
            taxonomy=self._taxonomy,
            reviewer_version=self.reviewer_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd_cost=usd_cost,
            latency_ms=latency_ms,
        )

    def estimate_usd(self, *, input_tokens: int, output_tokens: int) -> float:
        """Public version of `_estimate_usd` so the cost guard can pre-check."""
        return self._estimate_usd(input_tokens=input_tokens, output_tokens=output_tokens)

    def _estimate_usd(self, *, input_tokens: int, output_tokens: int) -> float:
        prices = PRICING_PER_1M.get(self.model, PRICING_PER_1M["deepseek-chat"])
        return round(
            input_tokens / 1_000_000 * prices["input"]
            + output_tokens / 1_000_000 * prices["output"],
            6,
        )


# ── parsing helpers ─────────────────────────────────────────────────────


def _payload_from_parsed(
    *,
    parsed: dict[str, Any],
    cap: AwarenessCaptureView,
    taxonomy: Taxonomy,
    reviewer_version: str,
    input_tokens: int,
    output_tokens: int,
    usd_cost: float,
    latency_ms: int,
) -> ReviewPayload:
    """Normalize the DeepSeek JSON object onto `ReviewPayload`.

    Filters labels through the taxonomy allow-list, clamps scores to
    [0,1], and silently drops fields the model invented outside the
    schema. The compare page diffs raw output anyway via `raw_response`.
    """
    valid_assets = set(taxonomy.asset_class_ids)
    valid_reasons = set(taxonomy.reason_code_ids)
    asset_classes = tuple(
        x for x in _as_str_list(parsed.get("asset_classes")) if x in valid_assets
    )
    reason_codes = tuple(
        x for x in _as_str_list(parsed.get("impact_reason_codes")) if x in valid_reasons
    )
    symbols = tuple(_as_str_list(parsed.get("candidate_symbols"))[:8])
    evidence = tuple(_as_str_list(parsed.get("evidence_sentences"))[:3])

    sentiment_label_raw = parsed.get("sentiment_label")
    sentiment_label = (
        sentiment_label_raw
        if isinstance(sentiment_label_raw, str)
        and sentiment_label_raw in {"positive", "neutral", "negative"}
        else None
    )

    return ReviewPayload(
        capture_id=cap.capture_id,
        reviewer_id=REVIEWER_DEEPSEEK,
        reviewer_version=reviewer_version,
        is_finance_relevant=_as_bool(parsed.get("is_finance_relevant")),
        finance_relevance_score=_as_clamped_float(parsed.get("finance_relevance_score")),
        asset_classes=asset_classes,
        impact_reason_codes=reason_codes,
        candidate_symbols=symbols,
        sentiment_label=sentiment_label,
        sentiment_score=_as_optional_clamped_float(parsed.get("sentiment_score")),
        evidence_sentences=evidence,
        reason_text=(parsed.get("reason_text") if isinstance(parsed.get("reason_text"), str) else None),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd_cost=usd_cost,
        latency_ms=latency_ms,
        raw_response=parsed,
    )


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _as_clamped_float(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _as_optional_clamped_float(value: Any) -> float | None:
    if value is None:
        return None
    return _as_clamped_float(value)


# ── Streaming narrative helper ──────────────────────────────────────────
#
# Used by /api/quant/live-read-stream to surface DeepSeek tokens to the
# UI chunk-by-chunk (typing effect). Separate from the JSON-mode `review()`
# call above — the streaming path is plain-text completion (no JSON parse)
# and the response is iterated SSE-style on the wire.

# Sentinel returned from `aiter_lines()` when DeepSeek closes the stream.
_STREAM_DONE_MARKER = "[DONE]"


async def stream_chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.35,
    max_tokens: int = 320,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async-yield envelopes from DeepSeek's streaming chat-completions API.

    Each yielded dict is one of:
        {"type": "delta", "text": str}        # content chunk
        {"type": "usage", "usage": {...}}     # final token usage (optional)
        {"type": "error", "error": str}       # transport / non-200 / parse
        {"type": "done"}                      # graceful end of stream

    The function NEVER raises — every failure mode is surfaced as an
    error envelope, so callers can keep the SSE wire alive long enough
    to emit a meaningful error event to the browser.

    `client` may be injected by tests to swap in a mock transport. When
    omitted, we open a short-lived AsyncClient and close it on exit.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    try:
        async with client.stream("POST", url, json=request_body, headers=headers) as response:
            if response.status_code != 200:
                body = await response.aread()
                snippet = body.decode("utf-8", errors="replace")[:200]
                yield {"type": "error", "error": f"http_{response.status_code}: {snippet}"}
                return
            async for raw_line in response.aiter_lines():
                if not raw_line:
                    continue
                line = raw_line.strip()
                # DeepSeek emits OpenAI-style SSE: "data: {json}" or
                # "data: [DONE]". Heartbeat / comment lines start with ":"
                # — ignore them.
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == _STREAM_DONE_MARKER:
                    yield {"type": "done"}
                    return
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    # Mid-stream parse failures shouldn't kill the whole
                    # connection — skip the bad frame and keep reading.
                    continue
                choices = parsed.get("choices") or []
                if choices:
                    delta = (choices[0] or {}).get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield {"type": "delta", "text": content}
                usage = parsed.get("usage")
                if isinstance(usage, dict) and usage:
                    yield {"type": "usage", "usage": usage}
    except httpx.TimeoutException as exc:
        yield {"type": "error", "error": f"timeout: {exc}"}
    except httpx.HTTPError as exc:
        yield {"type": "error", "error": f"transport: {exc}"}
    except Exception as exc:  # pragma: no cover — defensive
        yield {"type": "error", "error": f"unexpected: {exc}"[:200]}
    finally:
        if own_client and client is not None:
            await client.aclose()
