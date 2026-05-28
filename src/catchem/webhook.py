"""Slack/Discord/Teams-compatible incoming-webhook output.

Wired into the supervisor as a fire-and-forget background hook: when a
finalized `FinancialImpactRecord` clears the configured score floor (and
the optional asset-class / reason-code filters), `send_webhook(record,
config)` is dispatched on the supervisor's thread pool.

The webhook URL is a soft secret — Slack/Discord/Teams encode an auth
token in the path. It is held in sidecar memory only and is intentionally
omitted from the workspace-snapshot export (see frontend SNAPSHOT_ALLOW_LIST).

Errors NEVER propagate back to the caller. A bad URL, a 404 from Slack, a
timeout, or a malformed payload all return `(False, "...")` and are
silently swallowed by the supervisor wrapper after a structured log line.

SSRF defense:
  The sidecar runs on the user's machine with access to the local
  network, the cloud-metadata service (169.254.169.254 on AWS / GCP /
  Azure), and any service bound to localhost. A misconfigured webhook
  URL pointing at one of those targets would let an attacker exfiltrate
  per-record payloads (titles, evidence sentences, symbols) to anything
  reachable from the host. ``is_safe_webhook_url`` rejects every IPv4/IPv6
  address space that should never be a legitimate webhook destination:
  private, loopback, link-local (incl. AWS metadata), multicast,
  unspecified. Hostnames known to resolve to those ranges (``localhost``,
  ``*.local``) are rejected pre-DNS. Real publisher hostnames (slack,
  discord, teams) all resolve to public ranges and pass cleanly.
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

import httpx

from .logging import get_logger
from .settings import WebhookConfig

logger = get_logger("catchem.webhook")

# Acceptable URL schemes. Anything else gets refused at the API surface
# AND at send time so a misconfigured .env can't punch through.
_ALLOWED_SCHEMES = ("http://", "https://")

# Hostnames that resolve to loopback or otherwise un-safe ranges on
# every platform. Reject pre-DNS so a sidecar without resolver access
# still blocks them.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})


def is_safe_webhook_url(url: str) -> bool:
    """Reject URLs that target the host's loopback, private LAN, or metadata.

    The webhook sink is invoked from the sidecar process which has
    untrammelled network access (no firewall sits between it and the
    LAN, between it and 127.0.0.0/8, or between it and the cloud
    metadata service at 169.254.169.254). A misconfigured URL — set
    via the Settings page or an env var that escaped review — can
    therefore exfiltrate every high-scoring record's title + symbols
    to whatever is bound on the operator's machine.

    Rules:
      * scheme MUST be http or https (already enforced upstream — re-checked
        here as defense-in-depth so this function alone is a safe gate);
      * hostname MUST be present (``http://`` alone is rejected);
      * ``localhost``, ``ip6-localhost``, ``ip6-loopback`` are hard-rejected;
      * any hostname ending in ``.local`` is rejected (mDNS / Bonjour);
      * if the hostname parses as an IP literal, we apply the full
        ``ipaddress`` taxonomy:
          - ``is_private``     (RFC 1918 / RFC 4193 / RFC 5737)
          - ``is_loopback``    (127.0.0.0/8 + ::1)
          - ``is_link_local``  (169.254.0.0/16 + fe80::/10 — covers AWS metadata)
          - ``is_multicast``
          - ``is_unspecified`` (0.0.0.0 + ::)
        any True flag rejects the URL.
      * real hostnames (slack.com, discord.com, hooks.foo.io) that
        resolve to public space pass — we do NOT add a DNS-resolution
        step because (a) it adds latency on every send_webhook /
        config-save call, and (b) DNS results change between
        write-time and send-time so a TOCTOU check would not actually
        plug the hole anyway. The hostname-string rejection above
        catches the well-known textual bypasses.

    Returns True only when the URL passes every check.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    host = host.lower()
    if host in _BLOCKED_HOSTNAMES:
        return False
    if host.endswith(".local"):
        return False
    # IP literal? Run the full taxonomy. ip_address handles both IPv4
    # and IPv6, and rejects malformed strings via ValueError — those
    # are necessarily hostnames, which we accept (DNS-time defense is
    # not feasible — see docstring).
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    return True


def is_valid_webhook_url(url: str) -> bool:
    """Cheap sanity check: must look like an http(s) URL with a host AND be safe.

    Used by the API surface to reject obviously-broken URLs at write time
    rather than discovering them at first send. Empty string is rejected
    here — the caller decides whether empty means "no webhook" or "error".

    The safety filter (``is_safe_webhook_url``) prevents the sidecar from
    being weaponised into an SSRF probe against the operator's LAN /
    loopback / cloud-metadata service. Composed here so EVERY entry point
    that gates on "valid" automatically also gates on "safe".
    """
    if not isinstance(url, str) or not url:
        return False
    if not url.startswith(_ALLOWED_SCHEMES):
        return False
    # Reject "https://" alone — there must be a host after the scheme.
    rest = url.split("://", 1)[1] if "://" in url else ""
    if not rest:
        return False
    # Belt-and-suspenders: the SSRF gate already covers hostname presence
    # via urlparse, but keep the "." or "localhost" cheap shape check so a
    # ``https://example`` (no TLD) doesn't slip past to network code.
    # ``localhost`` itself is rejected by is_safe_webhook_url, so this
    # only allows hostnames that look like real DNS names.
    if not ("." in rest or rest.startswith("localhost")):
        return False
    return is_safe_webhook_url(url)


def build_slack_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Build a Slack-compatible incoming-webhook payload.

    The same shape works on Discord (it accepts Slack-compatible payloads
    on its `?wait=true` endpoint or via the explicit Slack-compat URL),
    and on Teams via an "Incoming Webhook" connector that renders the
    `text` field at minimum. We always include `blocks` so Slack-native
    consumers get the richer card, while non-Slack consumers fall back to
    `text` cleanly.
    """
    title = record.get("title") or "(no title)"
    url = record.get("url") or ""
    domain = record.get("domain") or "unknown"
    score = float(record.get("finance_relevance_score") or 0.0)
    asset_classes = ", ".join(record.get("asset_classes") or [])
    reason_codes = ", ".join(record.get("impact_reason_codes") or [])
    symbols = ", ".join((record.get("candidate_symbols") or [])[:5])

    # Slack truncates >3000 chars per block element. Titles in the wild
    # stay well under that, but we slice defensively so a freakishly
    # long headline never trips Slack's "invalid_block" error.
    safe_title = title[:280]
    header_text = (
        f"*<{url}|{safe_title}>*" if url else f"*{safe_title}*"
    ) + f"\nDomain: `{domain}` · Score: *{score:.2f}*"
    context_text = f"{asset_classes or '—'} · {reason_codes or '—'}"
    if symbols:
        context_text += f" · {symbols}"

    return {
        "text": f"High-relevance: {safe_title}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_text},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": context_text}],
            },
        ],
    }


def should_send(record: dict[str, Any], config: WebhookConfig) -> bool:
    """Filter gate: enabled + URL configured + score floor + label filters."""
    if not config.enabled or not config.url:
        return False
    if not is_valid_webhook_url(config.url):
        return False
    score = float(record.get("finance_relevance_score") or 0.0)
    if score < config.min_score:
        return False
    if config.asset_class_filter:
        rec_classes = record.get("asset_classes") or []
        if not any(ac in rec_classes for ac in config.asset_class_filter):
            return False
    if config.reason_code_filter:
        rec_reasons = record.get("impact_reason_codes") or []
        if not any(rc in rec_reasons for rc in config.reason_code_filter):
            return False
    return True


def send_webhook(record: dict[str, Any], config: WebhookConfig) -> tuple[bool, str]:
    """Send a single webhook. Returns `(success, status_or_error_code)`.

    Never raises. The supervisor schedules this on a background thread
    and discards the result. Status codes:
        ("sent", "filtered", "timeout", "http_NNN",
         "error_<exc>", "invalid_url")
    """
    if not should_send(record, config):
        # Distinguish "URL invalid" from "filtered" so test surfaces are
        # honest about the rejection reason.
        if config.enabled and config.url and not is_valid_webhook_url(config.url):
            return False, "invalid_url"
        return False, "filtered"
    try:
        payload = build_slack_payload(record)
        response = httpx.post(
            config.url,
            json=payload,
            timeout=config.timeout_seconds,
        )
        if response.status_code < 400:
            return True, "sent"
        return False, f"http_{response.status_code}"
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "webhook_send_failed",
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        return False, f"error_{type(exc).__name__}"


__all__ = [
    "build_slack_payload",
    "is_safe_webhook_url",
    "is_valid_webhook_url",
    "send_webhook",
    "should_send",
]
