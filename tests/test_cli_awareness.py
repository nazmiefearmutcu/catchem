"""Coverage for ``catchem awareness`` — the terminal twin of /api/news/awareness.

Two modes:
  * STATIC (default): imports the configured feed set directly + tallies by
    parser. Offline — no sidecar, no network. Asserts the broad source count
    (>=180) + per-parser breakdown render, exit 0, and a parseable --json
    envelope.
  * --live: monkeypatches httpx.get to return a stub awareness payload and
    asserts the live window renders; the unreachable path → exit 2.

Built on the autouse ``isolated_env`` fixture in conftest.py (same as
tests/test_cli.py / tests/test_cli_coverage.py) so no real DB / network is
touched.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from catchem.cli import app

runner = CliRunner()


# ── static mode (no sidecar, no network) ────────────────────────────────────


def test_awareness_static_lists_sources_and_parsers() -> None:
    """Default text output: broad source total + per-parser breakdown, exit 0."""
    result = runner.invoke(app, ["awareness"])
    assert result.exit_code == 0, result.output
    assert "catchem awareness" in result.output
    assert "sources by parser:" in result.output
    # The curated DEFAULT_FEEDS + source packs assemble to a broad set.
    assert "poll every" in result.output
    # rss is always present (the built-in parser); breadth side must show it.
    assert "rss" in result.output


def test_awareness_static_source_count_is_broad() -> None:
    """The static tally must reflect the broad configured surface (>=180)."""
    result = runner.invoke(app, ["awareness", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    assert payload["sources_total"] >= 180, payload["sources_total"]
    # The JSON source count and the breakdown sum must agree.
    assert sum(payload["sources_by_parser"].values()) == payload["sources_total"]


def test_awareness_static_json_envelope() -> None:
    """--json emits a parseable envelope mirroring the HTTP twin's breadth keys."""
    result = runner.invoke(app, ["awareness", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    for key in ("schema_version", "configured", "sources_total",
                "sources_by_parser", "poll_interval_seconds"):
        assert key in payload, f"missing {key} in JSON payload"
    assert payload["configured"] is True
    assert isinstance(payload["sources_by_parser"], dict)
    # rss is the built-in parser key and must always be tallied.
    assert payload["sources_by_parser"].get("rss", 0) > 0
    assert isinstance(payload["poll_interval_seconds"], (int, float))


# ── --live mode (stubbed httpx) ─────────────────────────────────────────────


def _stub_awareness_payload() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-05-28T15:36:03Z",
        "configured": True,
        "sources_total": 282,
        "sources_by_parser": {"rss": 260, "twitter": 12, "reddit": 7, "gdelt": 3},
        "poll_interval_seconds": 10.0,
        "median_publisher_lag_seconds": 624.0,
        "avg_publisher_lag_seconds": 701.5,
        "last_run_at": "2026-05-28T15:35:55Z",
        "last_new_at": "2026-05-28T15:35:55Z",
        "total_ingested": 1487,
        "window_estimate_seconds": 634.0,
    }


def test_awareness_live_renders_window(monkeypatch) -> None:
    """--live text output: sources, parser breakdown, lag, window, ingested."""
    import httpx

    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return _stub_awareness_payload()

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResp())

    result = runner.invoke(app, ["awareness", "--live"])
    assert result.exit_code == 0, result.output
    assert "live" in result.output
    assert "282 sources" in result.output
    # parser breakdown sorted by count desc → rss first
    rss_idx = result.output.find("rss")
    tw_idx = result.output.find("twitter")
    assert rss_idx >= 0 and tw_idx >= 0 and rss_idx < tw_idx
    # freshness block surfaces median lag + window + ingested total
    assert "median publisher lag" in result.output
    assert "624s" in result.output
    assert "effective window" in result.output
    assert "1,487" in result.output


def test_awareness_live_json_passthrough(monkeypatch) -> None:
    """--live --json returns the raw HTTP payload (script-friendly)."""
    import httpx

    expected = _stub_awareness_payload()

    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return expected

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResp())

    result = runner.invoke(app, ["awareness", "--live", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    assert json.loads(body) == expected


def test_awareness_live_handles_unreachable_sidecar(monkeypatch) -> None:
    """No sidecar listening → exit code 2 + actionable error message."""
    import httpx

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "get", _boom)

    result = runner.invoke(app, ["awareness", "--live"])
    assert result.exit_code == 2, result.output
    assert "sidecar unreachable" in result.output
    assert "catchem serve" in result.output


def test_awareness_live_unreachable_json(monkeypatch) -> None:
    """--live --json on unreachable sidecar emits an error envelope, exit 2."""
    import httpx

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "get", _boom)

    result = runner.invoke(app, ["awareness", "--live", "--json"])
    assert result.exit_code == 2, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    assert payload["ok"] is False
    assert "unreachable" in payload["error"]
