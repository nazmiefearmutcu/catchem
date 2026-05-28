"""Webhook output tests — Slack/Discord/Teams-compatible POST.

Covers the pure-logic surface (`should_send`, `build_slack_payload`) plus
the HTTP layer (`send_webhook`) with the supervisor's process running
through the API surface for the round-trip endpoints.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import WebhookConfig, load_settings, reload_settings
from catchem.webhook import (
    build_slack_payload,
    is_safe_webhook_url,
    is_valid_webhook_url,
    send_webhook,
    should_send,
)

SAMPLE_RECORD: dict = {
    "capture_id": "cap-001",
    "title": "Fed lifts rates 25 bps; equities slide",
    "url": "https://reuters.com/article/fed-hike",
    "domain": "reuters.com",
    "finance_relevance_score": 0.85,
    "asset_classes": ["equities", "rates"],
    "impact_reason_codes": ["central_bank", "earnings"],
    "candidate_symbols": ["SPY", "TLT", "AAPL"],
}


# ── should_send filters ──────────────────────────────────────────────────


def test_should_send_disabled_returns_false() -> None:
    cfg = WebhookConfig(enabled=False, url="https://hooks.slack.com/x", min_score=0.5)
    assert should_send(SAMPLE_RECORD, cfg) is False


def test_should_send_respects_min_score() -> None:
    low = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.9)
    assert should_send(SAMPLE_RECORD, low) is False  # 0.85 < 0.9
    high = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.5)
    assert should_send(SAMPLE_RECORD, high) is True


def test_should_send_respects_asset_class_filter_any_match() -> None:
    matched = WebhookConfig(
        enabled=True,
        url="https://hooks.slack.com/x",
        min_score=0.5,
        asset_class_filter=["crypto", "equities"],  # equities matches
    )
    assert should_send(SAMPLE_RECORD, matched) is True

    unmatched = WebhookConfig(
        enabled=True,
        url="https://hooks.slack.com/x",
        min_score=0.5,
        asset_class_filter=["crypto", "fx"],
    )
    assert should_send(SAMPLE_RECORD, unmatched) is False


def test_should_send_respects_reason_code_filter() -> None:
    matched = WebhookConfig(
        enabled=True,
        url="https://hooks.slack.com/x",
        min_score=0.5,
        reason_code_filter=["central_bank"],
    )
    assert should_send(SAMPLE_RECORD, matched) is True

    unmatched = WebhookConfig(
        enabled=True,
        url="https://hooks.slack.com/x",
        min_score=0.5,
        reason_code_filter=["litigation", "fraud_governance"],
    )
    assert should_send(SAMPLE_RECORD, unmatched) is False


def test_should_send_requires_valid_url() -> None:
    bad = WebhookConfig(enabled=True, url="ftp://nope.example", min_score=0.0)
    assert should_send(SAMPLE_RECORD, bad) is False
    good = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.0)
    assert should_send(SAMPLE_RECORD, good) is True


# ── URL validation ───────────────────────────────────────────────────────


def test_is_valid_webhook_url_scheme_check() -> None:
    assert is_valid_webhook_url("https://hooks.slack.com/T/B/secret") is True
    # Hardened (v45 critical-5): localhost/private/loopback/link-local are
    # now rejected because the sidecar's webhook sink would otherwise be an
    # SSRF primitive against the host. See is_safe_webhook_url docstring.
    assert is_valid_webhook_url("http://localhost:8080/hook") is False
    assert is_valid_webhook_url("ftp://example.com/x") is False
    assert is_valid_webhook_url("") is False
    assert is_valid_webhook_url("javascript:alert(1)") is False  # type: ignore[arg-type]
    assert is_valid_webhook_url("https://") is False


# ── SSRF guard (v45 critical-5) ──────────────────────────────────────────


def test_is_safe_webhook_url_blocks_loopback_hostname() -> None:
    """``localhost`` and IPv6 loopback aliases must be rejected pre-DNS.

    The sidecar holds an httpx client that resolves DNS itself; a
    hostname check must therefore land before resolution to be useful
    against an attacker whose resolver returns 127.0.0.1.
    """
    for url in (
        "http://localhost/hook",
        "http://localhost:8080/hook",
        "https://localhost:8443/secret",
        "http://ip6-localhost/hook",
        "http://ip6-loopback/hook",
    ):
        assert is_safe_webhook_url(url) is False, f"should reject {url}"
        assert is_valid_webhook_url(url) is False, f"is_valid must also reject {url}"


def test_is_safe_webhook_url_blocks_loopback_ipv4() -> None:
    """127.0.0.0/8 must be rejected; tests cover the whole block edges."""
    for url in (
        "http://127.0.0.1/hook",
        "http://127.0.0.1:9000/hook",
        "http://127.1.2.3/hook",         # mid-block — still loopback
        "http://127.255.255.254/hook",   # last usable address in /8
    ):
        assert is_safe_webhook_url(url) is False, f"should reject {url}"


def test_is_safe_webhook_url_blocks_ipv6_loopback() -> None:
    """``::1`` literals (with and without brackets) get rejected."""
    for url in (
        "http://[::1]/hook",
        "http://[::1]:8080/hook",
    ):
        assert is_safe_webhook_url(url) is False, f"should reject {url}"


def test_is_safe_webhook_url_blocks_private_ranges() -> None:
    """RFC 1918 + RFC 4193 — every analyst's LAN must be off-limits."""
    for url in (
        "http://10.0.0.1/hook",
        "http://10.255.255.254/hook",
        "http://172.16.0.1/hook",
        "http://172.31.255.254/hook",
        "http://192.168.1.1/hook",
        "http://192.168.0.42/hook",
        "http://[fd00::1]/hook",  # unique-local IPv6
    ):
        assert is_safe_webhook_url(url) is False, f"should reject {url}"


def test_is_safe_webhook_url_blocks_aws_metadata() -> None:
    """169.254.169.254 (AWS / GCP / Azure metadata) must be rejected.

    This is the canonical SSRF target — leaking a webhook to it returns
    IAM credentials on cloud runtimes. Even though catchem is local-first,
    a developer running the sidecar on a cloud workstation must be safe.
    """
    assert is_safe_webhook_url("http://169.254.169.254/latest/meta-data/") is False
    # Other link-local IPv4 — same /16, still rejected.
    assert is_safe_webhook_url("http://169.254.1.1/") is False
    # IPv6 link-local
    assert is_safe_webhook_url("http://[fe80::1]/hook") is False


def test_is_safe_webhook_url_blocks_multicast_and_unspecified() -> None:
    """Multicast (224.0.0.0/4) + 0.0.0.0 / :: are non-targets too."""
    for url in (
        "http://224.0.0.1/hook",
        "http://239.255.255.250/hook",
        "http://0.0.0.0/hook",
        "http://[::]/hook",
    ):
        assert is_safe_webhook_url(url) is False, f"should reject {url}"


def test_is_safe_webhook_url_blocks_mdns_local() -> None:
    """``*.local`` (Bonjour/mDNS) — typically resolves to LAN, reject."""
    for url in (
        "http://printer.local/hook",
        "https://my-mac.local/secret",
    ):
        assert is_safe_webhook_url(url) is False, f"should reject {url}"


def test_is_safe_webhook_url_accepts_public_hostnames() -> None:
    """Real publishers must not regress — Slack / Discord / Teams / GitHub."""
    for url in (
        "https://hooks.slack.com/services/T/B/secret",
        "https://discord.com/api/webhooks/12345/abc",
        "https://outlook.office.com/webhook/abcdef",
        "https://api.github.com/repos/x/y/dispatches",
        "https://example.com/hook",
        "https://example.co.uk/hook",
    ):
        assert is_safe_webhook_url(url) is True, f"should accept {url}"
        assert is_valid_webhook_url(url) is True, f"is_valid should also accept {url}"


def test_is_safe_webhook_url_accepts_public_ipv4() -> None:
    """A real public IPv4 literal is rare but legitimate (e.g. on-prem)."""
    assert is_safe_webhook_url("http://8.8.8.8/hook") is True
    assert is_safe_webhook_url("https://1.1.1.1/hook") is True


def test_is_safe_webhook_url_rejects_empty_and_malformed() -> None:
    """Guard against the obvious shape failures."""
    assert is_safe_webhook_url("") is False
    assert is_safe_webhook_url("not-a-url") is False
    assert is_safe_webhook_url("http://") is False
    assert is_safe_webhook_url("ftp://example.com/x") is False


# ── Slack payload shape ──────────────────────────────────────────────────


def test_build_slack_payload_includes_title_and_score() -> None:
    payload = build_slack_payload(SAMPLE_RECORD)
    assert "text" in payload
    assert "blocks" in payload
    assert payload["text"].startswith("High-relevance:")
    assert "Fed lifts rates" in payload["text"]
    blocks = payload["blocks"]
    assert blocks[0]["type"] == "section"
    section_text = blocks[0]["text"]["text"]
    assert "reuters.com" in section_text
    assert "0.85" in section_text  # score
    assert "https://reuters.com/article/fed-hike" in section_text
    context_text = blocks[1]["elements"][0]["text"]
    assert "equities" in context_text
    assert "central_bank" in context_text
    assert "SPY" in context_text


def test_build_slack_payload_handles_missing_fields() -> None:
    minimal: dict = {"finance_relevance_score": 0.5}
    payload = build_slack_payload(minimal)
    assert payload["text"]
    assert payload["blocks"]
    section_text = payload["blocks"][0]["text"]["text"]
    assert "(no title)" in section_text


# ── send_webhook HTTP layer (mocked httpx) ───────────────────────────────


def test_send_webhook_success() -> None:
    cfg = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.0)
    with patch("catchem.webhook.httpx.post") as post:
        post.return_value = httpx.Response(200, request=httpx.Request("POST", cfg.url))
        ok, status = send_webhook(SAMPLE_RECORD, cfg)
    assert ok is True
    assert status == "sent"
    # Verify the POST was made with a Slack-shape payload.
    call_kwargs = post.call_args.kwargs
    assert "json" in call_kwargs
    sent_payload = call_kwargs["json"]
    assert "blocks" in sent_payload


def test_send_webhook_timeout_returns_error() -> None:
    cfg = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.0)
    with patch("catchem.webhook.httpx.post") as post:
        post.side_effect = httpx.TimeoutException("read timeout")
        ok, status = send_webhook(SAMPLE_RECORD, cfg)
    assert ok is False
    assert status == "timeout"


def test_send_webhook_http_error_returns_status() -> None:
    cfg = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.0)
    with patch("catchem.webhook.httpx.post") as post:
        post.return_value = httpx.Response(404, request=httpx.Request("POST", cfg.url))
        ok, status = send_webhook(SAMPLE_RECORD, cfg)
    assert ok is False
    assert status == "http_404"


def test_send_webhook_filter_skip_does_not_call_post() -> None:
    cfg = WebhookConfig(enabled=True, url="https://hooks.slack.com/x", min_score=0.99)
    with patch("catchem.webhook.httpx.post") as post:
        ok, status = send_webhook(SAMPLE_RECORD, cfg)
    assert ok is False
    assert status == "filtered"
    post.assert_not_called()


# ── API round-trip via TestClient ────────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from catchem.rate_limit import reset_all_buckets

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    reload_settings()
    # Reset rate-limit buckets between tests so the /test endpoint's
    # cost=5 import-bucket doesn't leak across cases and cause spurious
    # 429s when multiple tests hit it within a minute.
    reset_all_buckets()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def test_api_webhook_config_get_default_disabled(client: TestClient) -> None:
    r = client.get("/api/webhook/config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False
    assert body["url_configured"] is False
    # URL itself is never shipped back
    assert "url" not in body or not body.get("url")
    assert body["min_score"] == 0.7
    assert body["stats"]["attempted"] == 0


def test_api_webhook_config_round_trip(client: TestClient) -> None:
    r = client.post(
        "/api/webhook/config",
        json={
            "enabled": True,
            "url": "https://hooks.slack.com/services/T/B/secret",
            "min_score": 0.6,
            "asset_class_filter": ["equities", "crypto"],
            "reason_code_filter": ["central_bank"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["url_configured"] is True
    assert body["min_score"] == pytest.approx(0.6)
    assert body["asset_class_filter"] == ["equities", "crypto"]
    assert body["reason_code_filter"] == ["central_bank"]
    # GET reads the same state
    g = client.get("/api/webhook/config").json()
    assert g["enabled"] is True
    assert g["url_configured"] is True


def test_api_webhook_config_rejects_invalid_url(client: TestClient) -> None:
    r = client.post("/api/webhook/config", json={"url": "ftp://example.com/x"})
    assert r.status_code == 422
    assert "http" in r.json()["detail"]


def test_api_webhook_config_clamps_min_score(client: TestClient) -> None:
    r = client.post("/api/webhook/config", json={"min_score": 3.5})
    assert r.status_code == 200
    assert r.json()["min_score"] == pytest.approx(1.0)
    r = client.post("/api/webhook/config", json={"min_score": -1.0})
    assert r.status_code == 200
    assert r.json()["min_score"] == pytest.approx(0.0)


def test_api_webhook_test_requires_url(client: TestClient) -> None:
    """No URL configured → friendly 200 envelope (not a 422).

    The SPA's WebhookOutputCard branches on the JSON body shape
    (`ok` + `status`) instead of having to parse error responses, so the
    endpoint stays callable and never raises. The button is disabled
    client-side in this state, but curl probes still get a usable answer.
    """
    r = client.post("/api/webhook/test", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "no_url_configured"
    assert body["url_configured"] is False


def test_api_webhook_test_fires_post(client: TestClient) -> None:
    client.post(
        "/api/webhook/config",
        json={"url": "https://hooks.slack.com/services/T/B/secret"},
    )
    with patch("catchem.webhook.httpx.post") as post:
        post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "https://x")
        )
        r = client.post("/api/webhook/test", json={"title": "ping"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "sent"
    post.assert_called_once()
    sent_payload = post.call_args.kwargs["json"]
    assert "ping" in sent_payload["text"]


def test_api_webhook_test_http_500_returns_status(client: TestClient) -> None:
    """Backend reports the upstream HTTP failure as `http_500` without raising.

    The UI flips that into a red "✗ Test failed: http_500" chip — the test
    endpoint is the only surface where we can observe per-call delivery
    status, so this contract is load-bearing for the Settings card.
    """
    client.post(
        "/api/webhook/config",
        json={"url": "https://hooks.slack.com/services/T/B/secret"},
    )
    with patch("catchem.webhook.httpx.post") as post:
        post.return_value = httpx.Response(
            500, request=httpx.Request("POST", "https://x")
        )
        r = client.post("/api/webhook/test", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "http_500"
    assert body["url_configured"] is True


def test_api_webhook_test_bypasses_score_and_filters(client: TestClient) -> None:
    """Test path force-bypasses min_score + label filters.

    The operator-facing intent is "verify the URL works" — gating that on
    a configured min_score=0.99 + obscure asset/reason filters would make
    the button useless in production-tuned setups. The synthetic record
    carries `score=0.99` and gets sent through a relaxed WebhookConfig
    regardless of what the user has saved on the config side.
    """
    client.post(
        "/api/webhook/config",
        json={
            "url": "https://hooks.slack.com/services/T/B/secret",
            # Aggressive filters that would normally reject everything:
            "min_score": 1.0,
            "asset_class_filter": ["macro"],
            "reason_code_filter": ["natural_disaster"],
        },
    )
    with patch("catchem.webhook.httpx.post") as post:
        post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "https://x")
        )
        r = client.post("/api/webhook/test", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "sent"
    post.assert_called_once()  # filters did not gate the send


def test_api_webhook_test_fires_even_when_disabled(client: TestClient) -> None:
    """`enabled=false` should NOT prevent the test from firing.

    Testing connectivity before flipping the master switch is the whole
    point of the button. The test_cfg the endpoint builds always has
    `enabled=True` so the gate inside `send_webhook` doesn't short-circuit
    on the user's saved disabled state.
    """
    client.post(
        "/api/webhook/config",
        json={
            "url": "https://hooks.slack.com/services/T/B/secret",
            "enabled": False,
        },
    )
    with patch("catchem.webhook.httpx.post") as post:
        post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "https://x")
        )
        r = client.post("/api/webhook/test", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "sent"
    post.assert_called_once()
