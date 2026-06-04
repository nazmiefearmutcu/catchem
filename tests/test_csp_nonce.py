"""Per-request nonce-based CSP for the SPA shell.

The SPA's FOUC theme bootstrap is an inline <script>. Rather than relax
`script-src` to `'unsafe-inline'`, every HTML response for the SPA stamps
a fresh nonce onto the inline tag and emits a matching
`script-src 'self' 'nonce-<random>'` directive.

These tests pin the contract:
  1. GET / serves CSP with a nonce; the same nonce appears on every
     <script> tag in the body.
  2. The nonce is fresh per request (no cross-request reuse).
  3. The SPA CSP NEVER contains 'unsafe-inline' on script-src.
  4. Non-HTML responses still get the strict middleware default CSP
     (with 'unsafe-inline' kept on style-src only) and no nonce header.
  5. The SPA history catch-all (/anything) also serves a nonced response.
  6. The /replay SPA route is nonced.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


_NONCE_DIRECTIVE_RE = re.compile(r"'nonce-([A-Za-z0-9_\-]+)'")
_SCRIPT_TAG_NONCE_RE = re.compile(r'<script[^>]*\bnonce="([A-Za-z0-9_\-]+)"', re.I)


def _extract_csp_nonce(csp: str) -> str | None:
    m = _NONCE_DIRECTIVE_RE.search(csp)
    return m.group(1) if m else None


def test_root_csp_carries_nonce_matching_every_script(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

    body = r.text
    # The SPA shell ships the bundle in <div id="root">; if the bundle isn't
    # built (a possible CI shape) the placeholder ships no inline scripts —
    # in that case the no-nonce path is exercised and there's nothing to
    # check here. Production builds bundle the SPA so this skip is rare.
    if '<div id="root"></div>' not in body:
        pytest.skip("SPA bundle not built; placeholder has no inline scripts")

    csp = r.headers.get("Content-Security-Policy", "")
    nonce = _extract_csp_nonce(csp)
    assert nonce is not None, f"CSP missing nonce directive: {csp!r}"
    # nonce is base64url of 16 random bytes → 22 chars
    assert len(nonce) >= 16

    script_nonces = _SCRIPT_TAG_NONCE_RE.findall(body)
    assert script_nonces, "no <script nonce=...> tags found in SPA body"
    assert all(n == nonce for n in script_nonces), (
        f"script nonces {script_nonces!r} don't all match CSP nonce {nonce!r}"
    )


def test_root_nonce_is_fresh_per_request(client: TestClient) -> None:
    r1 = client.get("/")
    r2 = client.get("/")
    if '<div id="root"></div>' not in r1.text:
        pytest.skip("SPA bundle not built")
    n1 = _extract_csp_nonce(r1.headers.get("Content-Security-Policy", ""))
    n2 = _extract_csp_nonce(r2.headers.get("Content-Security-Policy", ""))
    assert n1 and n2 and n1 != n2, f"nonce reused across requests: {n1!r} == {n2!r}"


def test_spa_csp_drops_unsafe_inline_from_script_src(client: TestClient) -> None:
    r = client.get("/")
    if '<div id="root"></div>' not in r.text:
        pytest.skip("SPA bundle not built")
    csp = r.headers.get("Content-Security-Policy", "")
    # Pull out the script-src directive only — style-src may legitimately
    # carry 'unsafe-inline' for React inline styles.
    m = re.search(r"script-src([^;]*);", csp)
    assert m is not None, f"no script-src directive in CSP: {csp!r}"
    script_src = m.group(1)
    assert "'unsafe-inline'" not in script_src, (
        f"script-src still allows 'unsafe-inline': {script_src!r}"
    )
    assert "'self'" in script_src and "'nonce-" in script_src


def test_non_html_response_keeps_default_csp_no_nonce(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy", "")
    assert csp, "non-HTML response missing CSP entirely"
    # The middleware default does NOT carry a nonce.
    assert _NONCE_DIRECTIVE_RE.search(csp) is None, (
        f"unexpected nonce on non-HTML response: {csp!r}"
    )
    # Default still pins object-src/frame-ancestors hard.
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_middleware_default_csp_has_no_unsafe_inline_on_script_src(
    client: TestClient,
) -> None:
    """The default CSP applied by middleware (for non-HTML responses and
    error responses like HEAD→405 or 404) must NEVER advertise
    'unsafe-inline' on script-src. Only style-src may carry it for React's
    inline style attributes. A permissive default would contradict the
    nonce-only policy on HTML GETs and mislead operators running smoke
    probes like `curl -sI /`.
    """
    # /healthz hits the middleware default directly (no route-level CSP).
    r = client.get("/healthz")
    csp = r.headers.get("Content-Security-Policy", "")
    m = re.search(r"script-src([^;]*);", csp)
    assert m is not None, f"no script-src directive in default CSP: {csp!r}"
    script_src = m.group(1)
    assert "'unsafe-inline'" not in script_src, (
        f"middleware default script-src must not allow 'unsafe-inline': {script_src!r}"
    )
    assert "'self'" in script_src


def test_head_on_root_does_not_leak_unsafe_inline_script_src(
    client: TestClient,
) -> None:
    """A HEAD probe against `/` returns 405 (route is GET-only) and falls
    through to the middleware default CSP. Even there, script-src must
    not advertise 'unsafe-inline' — operators routinely run
    `curl -sI http://host/` as a smoke check and would otherwise see
    a misleading permissive policy.
    """
    r = client.head("/")
    assert r.status_code == 405
    csp = r.headers.get("Content-Security-Policy", "")
    assert csp, "405 response missing CSP entirely"
    m = re.search(r"script-src([^;]*);", csp)
    assert m is not None, f"no script-src directive in 405 CSP: {csp!r}"
    script_src = m.group(1)
    assert "'unsafe-inline'" not in script_src, (
        f"HEAD/405 script-src must not allow 'unsafe-inline': {script_src!r}"
    )


def test_spa_history_fallback_is_nonced(client: TestClient) -> None:
    """A history-mode route like /feed must also serve a nonced SPA shell."""
    r = client.get("/feed")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    if '<div id="root"></div>' not in r.text:
        pytest.skip("SPA bundle not built")
    csp = r.headers.get("Content-Security-Policy", "")
    nonce = _extract_csp_nonce(csp)
    assert nonce is not None, f"history fallback CSP missing nonce: {csp!r}"
    script_nonces = _SCRIPT_TAG_NONCE_RE.findall(r.text)
    assert script_nonces and all(n == nonce for n in script_nonces)


def test_replay_spa_route_is_nonced(client: TestClient) -> None:
    """GET /replay also serves the SPA shell with a fresh nonce."""
    r = client.get("/replay")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    if '<div id="root"></div>' not in r.text:
        pytest.skip("SPA bundle not built")
    csp = r.headers.get("Content-Security-Policy", "")
    nonce = _extract_csp_nonce(csp)
    assert nonce is not None
    script_nonces = _SCRIPT_TAG_NONCE_RE.findall(r.text)
    assert script_nonces and all(n == nonce for n in script_nonces)
