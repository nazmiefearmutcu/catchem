"""Supplemental coverage for ``catchem.cli`` commands not exercised by
``test_cli.py`` / ``test_cli_tags.py``.

Scope additions over the existing two files:
  * Supervisor-backed top-level commands: ``run`` (replay), ``replay`` (with
    ``--path``), ``inspect`` (hit + miss), ``benchmark`` (throughput + golden),
    ``status`` (legacy Supervisor JSON), ``bootstrap-init``.
  * ``validate-guards`` — subprocess shelled out, return-code propagated.
  * ``demo`` — every input branch (``--text``, ``--text-file``, stdin, the
    "no body" error, and ``--json``).
  * Text-output paths the JSON-focused tests skip: ``top-recent`` text,
    ``db-stats`` text + indexes, ``db-info`` empty-records branch.
  * ``db-backup`` ``--json`` + default-name + directory-target.
  * ``search`` empty-query guard + ``--json`` clusters key.
  * ``export`` ``--min-score`` / ``--reason-code`` / ``--asset-class`` filters
    + missing-DB-safe + default output path.
  * ``quant-snapshot`` ``--output`` to a file.
  * ``ping-deepseek`` — no-key error, mocked HTTP success, mocked HTTP failure,
    mocked transport error. ``httpx.Client`` is always mocked so no live call
    is made, and key-requiring cases wire their own fake key instead of
    depending on a developer ``.env``.

All cases run through ``typer.testing.CliRunner`` and rely on the autouse
``isolated_env`` fixture (conftest.py) which points the SQLite path at
``tmp_path/data/db/`` per test. None of these tests touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from catchem.cli import app
from catchem.schemas import (
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.settings import load_settings
from catchem.storage import load_storage_from_settings

runner = CliRunner()


def _extract_json(output: str):
    """Parse the first JSON object in CLI output.

    Several commands print with ``indent=2`` (multi-line) and may have WARNING
    log lines before the payload, so a naive ``splitlines()[-1]`` fails. Find
    the first ``{`` and let ``raw_decode`` consume exactly one object, ignoring
    any trailing text (e.g. a "Wrote ..." confirmation line).
    """
    import json as _json

    start = output.find("{")
    assert start != -1, f"no JSON object in output: {output!r}"
    obj, _end = _json.JSONDecoder().raw_decode(output[start:])
    return obj


def _make_record(
    capture_id: str,
    title: str,
    domain: str,
    *,
    asset_classes: tuple[str, ...] = ("equities",),
    reason_codes: tuple[str, ...] = ("earnings",),
    symbols: tuple[str, ...] = ("AAPL",),
    score: float = 0.85,
) -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"doc-{capture_id}",
        title=title,
        text_excerpt=f"Body of {title}.",
        domain=domain,
        url=f"https://{domain}/{capture_id}",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=score,
        asset_classes=list(asset_classes),
        impact_reason_codes=list(reason_codes),
        candidate_symbols=list(symbols),
        candidate_entities=[],
        impact_horizons=["short_term"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.7,
        evidence_sentences=[f"Sentence about {title}."],
        reason_text=f"matched on {','.join(reason_codes)}",
        component_scores={"finance_relevance_score": score},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.PRODUCTION_SAFE,
        model_versions={"stub": "v0"},
    )


@pytest.fixture
def seeded_storage(tmp_path: Path):
    """Seed the per-test SQLite DB with a handful of records.

    Mirrors the seed in test_cli.py so the new cases share a known shape:
    4 finance-relevant records, mixed asset classes / reason codes / symbols.
    """
    settings = load_settings()
    storage = load_storage_from_settings(settings)
    fixtures = [
        _make_record("c-aapl", "Apple beats earnings expectations", "wsj.com"),
        _make_record(
            "c-msft", "Microsoft cloud growth accelerates", "reuters.com",
            symbols=("MSFT",),
        ),
        _make_record(
            "c-btc", "Bitcoin rallies past 80k on ETF inflows", "coindesk.com",
            asset_classes=("crypto",), reason_codes=("flow",), symbols=("BTC",),
        ),
        _make_record(
            "c-fed", "Fed holds rates steady amid stable inflation", "federalreserve.gov",
            asset_classes=("rates", "macro"), reason_codes=("central_bank",), symbols=(),
            score=0.92,
        ),
    ]
    for rec in fixtures:
        storage.insert_record(rec)
    storage.close()
    return settings


# ── top-level: run / replay (Supervisor-backed) ────────────────────────────


def test_run_replay_mode_emits_counts_json(tmp_path: Path) -> None:
    """`run` in replay mode prints a JSON counts envelope.

    The autouse fixture points AWARENESS_DATA_DIR at an empty tmp dir, so the
    replay processes zero records — but it must still complete and emit the
    `{"processed": ..., "skipped": ...}` JSON line, exit 0.
    """
    result = runner.invoke(app, ["run", "--mode", "replay_existing", "--max-records", "5"])
    assert result.exit_code == 0, result.output
    line = result.output.strip().split("\n")[-1]
    payload = json.loads(line)
    assert "processed" in payload
    assert isinstance(payload["processed"], int)


def test_replay_with_explicit_path(tmp_path: Path) -> None:
    """`replay --path FILE` repoints the data dir at the file's parent.

    We hand it an empty JSONL so the run is deterministic (zero processed)
    while still exercising the path-override branch (os.environ rewrite +
    reload_settings).
    """
    jsonl = tmp_path / "feed" / "captures.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["replay", "--path", str(jsonl)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert "processed" in payload


# ── top-level: inspect ──────────────────────────────────────────────────────


def test_inspect_prints_record_json(seeded_storage) -> None:
    result = runner.invoke(app, ["inspect", "--capture-id", "c-aapl"])
    assert result.exit_code == 0, result.output
    # The record JSON is pretty-printed; find the object portion.
    payload = _extract_json(result.output)
    assert payload["capture_id"] == "c-aapl"
    assert payload["title"] == "Apple beats earnings expectations"


def test_inspect_missing_record_exits_1(seeded_storage) -> None:
    result = runner.invoke(app, ["inspect", "--capture-id", "does-not-exist"])
    assert result.exit_code == 1
    assert "no record" in (result.output + (result.stderr or ""))


# ── top-level: benchmark ────────────────────────────────────────────────────


def test_benchmark_throughput_emits_rps(tmp_path: Path) -> None:
    """Default benchmark = throughput; prints counts + elapsed_s + rps."""
    result = runner.invoke(app, ["benchmark", "--limit", "5"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert "rps" in payload
    assert "elapsed_s" in payload
    assert "processed" in payload


def test_benchmark_golden_emits_report(tmp_path: Path) -> None:
    """`--golden` runs the curated set in-process and prints the report dict."""
    result = runner.invoke(app, ["benchmark", "--golden"])
    assert result.exit_code == 0, result.output
    # The golden report is a pretty-printed JSON object.
    payload = _extract_json(result.output)
    assert "relevance" in payload
    assert payload.get("n", 0) > 0


# ── command-name collision resolved: `status` vs `status-json` ─────────────


def test_status_name_resolves_to_one_liner(seeded_storage) -> None:
    """`status` must resolve to the one-line health summary (``cli_status``).

    Historically cli.py registered the name "status" TWICE — the early
    ``def status()`` dumping ``Supervisor.status()`` plus the later
    ``cli_status`` — so Typer silently kept only the last and the JSON command
    was dead/unreachable. The early command was renamed to "status-json" to end
    the collision; this pins that ``status`` still yields the one-liner.
    """
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    line = result.output.strip().split("\n")[-1]
    assert line.startswith("[catchem v")
    assert "records" in line
    # The one-liner must not emit the Supervisor JSON object.
    assert "{" not in result.output


def test_status_json_command_emits_supervisor_payload(seeded_storage) -> None:
    """The de-collided ``status-json`` command is now reachable and emits the
    Supervisor.status() payload — keys unique to it (mode, model_versions,
    reviewers) that the one-liner ``status`` does NOT surface."""
    result = runner.invoke(app, ["status-json"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert isinstance(payload, dict)
    for key in ("mode", "records", "model_versions", "reviewers"):
        assert key in payload, f"missing {key} in {payload!r}"


# ── top-level: bootstrap-init ───────────────────────────────────────────────


def test_bootstrap_init_emits_summary(tmp_path: Path) -> None:
    """bootstrap-init is idempotent; with --skip-warm it never touches HF.

    It must create the directory tree and print a JSON summary, exit 0.
    """
    result = runner.invoke(app, ["bootstrap-init"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert isinstance(payload, dict)
    assert payload  # non-empty summary


# ── top-level: validate-guards (subprocess) ─────────────────────────────────


def test_validate_guards_propagates_return_code(monkeypatch) -> None:
    """`validate-guards` shells out to verify_newsimpact_guard.py and exits with
    that process's return code. Mock subprocess.run so the test is hermetic and
    fast (no real verifier invocation)."""
    class _FakeProc:
        returncode = 0

    def _fake_run(_cmd, *a, **kw):
        return _FakeProc()

    # `subprocess` is imported lazily inside the command body; that binds the
    # real module object from sys.modules, so patching subprocess.run here is
    # what the command will see.
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", _fake_run)

    result = runner.invoke(app, ["validate-guards"])
    assert result.exit_code == 0, result.output


def test_validate_guards_nonzero_return_code(monkeypatch) -> None:
    """A non-zero verifier exit must propagate as the CLI exit code."""
    class _FakeProc:
        returncode = 3

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda *a, **kw: _FakeProc())

    result = runner.invoke(app, ["validate-guards"])
    assert result.exit_code == 3


# ── top-level: demo (all input branches) ────────────────────────────────────


def test_demo_with_inline_text_renders_report(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["demo", "--title", "Fed raises rates by 25 bps",
         "--text", "The Federal Reserve raised rates by 25 basis points citing inflation. Equities sold off.",
         "--domain", "reuters.com"],
    )
    assert result.exit_code == 0, result.output
    assert "demo capture:" in result.output


def test_demo_json_out_emits_record(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["demo", "-t", "Apple beats earnings",
         "--text", "Apple reported record quarterly revenue, beating analyst estimates.",
         "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload.get("title") == "Apple beats earnings"


def test_demo_reads_text_file(tmp_path: Path) -> None:
    body = tmp_path / "body.txt"
    body.write_text(
        "The European Central Bank held rates steady, surprising markets.",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["demo", "-t", "ECB holds", "--text-file", str(body), "--domain", "ft.com"],
    )
    assert result.exit_code == 0, result.output
    assert "demo capture:" in result.output


def test_demo_reads_stdin(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["demo", "-t", "Oil spikes on supply shock"],
        input="Crude oil futures jumped 6% after an unexpected supply disruption.",
    )
    assert result.exit_code == 0, result.output
    assert "demo capture:" in result.output


def test_demo_errors_without_body(tmp_path: Path) -> None:
    """No --text, no --text-file, and stdin is a TTY (CliRunner with no input)
    → exit code 2 with an actionable message."""
    result = runner.invoke(app, ["demo", "-t", "Headline only"])
    assert result.exit_code == 2
    assert "provide --text" in (result.output + (result.stderr or ""))


# ── db-info: empty-records (OperationalError) branch via fresh DB ───────────


def test_db_info_text_zero_records(tmp_path: Path) -> None:
    """A freshly-created Storage with no rows still reports a clean text block.

    Touch Storage so the file + schema exist (records=0), then run db-info.
    Exercises the text-output branch with a zero count.
    """
    settings = load_settings()
    storage = load_storage_from_settings(settings)
    storage.close()  # creates the file + DDL, leaves it empty

    result = runner.invoke(app, ["db-info"])
    assert result.exit_code == 0, result.output
    assert "Records:" in result.output
    assert "Schema version:" in result.output


# ── db-backup: --json + default-name + directory target ─────────────────────


def test_db_backup_json_envelope(seeded_storage, tmp_path: Path) -> None:
    target = tmp_path / "snap.sqlite3"
    result = runner.invoke(app, ["db-backup", "--output", str(target), "--json"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["backup"] == str(target.resolve())
    assert payload["size_bytes"] > 0
    assert target.exists()


def test_db_backup_into_directory_target(seeded_storage, tmp_path: Path) -> None:
    """When --output is an existing directory, a timestamped name is created
    inside it."""
    outdir = tmp_path / "backups"
    outdir.mkdir()
    result = runner.invoke(app, ["db-backup", "--output", str(outdir)])
    assert result.exit_code == 0, result.output
    made = list(outdir.glob("catchem-backup-*.sqlite3"))
    assert made, "no timestamped backup file created in directory target"


def test_db_backup_errors_when_db_missing(tmp_path: Path) -> None:
    """No Storage created yet → db file absent → exit 1."""
    result = runner.invoke(app, ["db-backup"])
    assert result.exit_code == 1
    assert "db not found" in (result.output + (result.stderr or ""))


# ── search: empty-query guard + json clusters key ──────────────────────────


def test_search_empty_query_exits_1(seeded_storage) -> None:
    result = runner.invoke(app, ["search", "   "])
    assert result.exit_code == 1
    assert "must not be empty" in (result.output + (result.stderr or ""))


def test_search_json_has_clusters_key(seeded_storage) -> None:
    result = runner.invoke(app, ["search", "Apple", "--json"])
    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    for key in ("query", "records", "symbols", "clusters"):
        assert key in payload
    assert isinstance(payload["clusters"], list)


def test_search_text_no_matches_empty_state(seeded_storage) -> None:
    """A query that matches nothing prints the '(no matches)' empty-state lines
    for each of the three sections."""
    result = runner.invoke(app, ["search", "zzz-no-such-token"])
    assert result.exit_code == 0, result.output
    assert result.output.count("(no matches)") >= 1


# ── export: filter branches + default output + missing-DB-safe ──────────────


def test_export_min_score_filters_rows(seeded_storage, tmp_path: Path) -> None:
    """--min-score drops the 0.85 records, keeping only the 0.92 Fed record."""
    target = tmp_path / "high.json"
    result = runner.invoke(
        app, ["export", "json", "--min-score", "0.9", "--output", str(target)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["items"][0]["capture_id"] == "c-fed"


def test_export_asset_class_and_reason_code_filters(seeded_storage, tmp_path: Path) -> None:
    target = tmp_path / "crypto.json"
    result = runner.invoke(
        app,
        ["export", "json", "--asset-class", "crypto", "--reason-code", "flow",
         "--output", str(target)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["items"][0]["capture_id"] == "c-btc"


def test_export_csv_default_output_path(seeded_storage, monkeypatch, tmp_path: Path) -> None:
    """With no --output, the file lands in CWD as catchem-records-<ts>.csv.

    Chdir into tmp_path so the auto-named file is created somewhere we control,
    then assert it exists.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export", "csv"])
    assert result.exit_code == 0, result.output
    made = list(tmp_path.glob("catchem-records-*.csv"))
    assert made, "auto-named CSV export not created in CWD"
    assert "Wrote" in result.output


# ── quant-snapshot: --output to a file ──────────────────────────────────────


def test_quant_snapshot_writes_to_file(seeded_storage, tmp_path: Path) -> None:
    target = tmp_path / "snap" / "quant.json"
    result = runner.invoke(
        app, ["quant-snapshot", "--limit", "50", "--output", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote quant snapshot" in result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "snapshot" in payload
    assert payload["limit"] == 50


# ── top-recent: text output (non-empty) ─────────────────────────────────────


def test_top_recent_text_lists_records(seeded_storage) -> None:
    """Default text output renders the score-prefixed lines for matches."""
    result = runner.invoke(app, ["top-recent", "--min-score", "0.5", "--limit", "10"])
    assert result.exit_code == 0, result.output
    # Highest-score record (Fed, 0.92) should appear, score-prefixed.
    assert "[0.92]" in result.output
    assert "Fed holds rates steady" in result.output


# ── db-stats: text output + indexes section ─────────────────────────────────


def test_db_stats_text_shows_indexes(seeded_storage) -> None:
    result = runner.invoke(app, ["db-stats"])
    assert result.exit_code == 0, result.output
    assert "tables:" in result.output
    # Seeded DB has at least one index → the indexes section header is emitted.
    assert "indexes:" in result.output
    assert "records" in result.output


# ── ping-deepseek: no-key, mocked success, mocked failure, transport error ──


def test_ping_deepseek_no_key_exits_1(monkeypatch) -> None:
    """With the api_key blanked, the command short-circuits to exit 1 before any
    HTTP. Patch the loaded settings' reviewer config so .env's real key can't
    leak into the test."""
    import catchem.cli as cli_mod

    real_load = cli_mod.load_settings

    def _load_no_key(*a, **kw):
        s = real_load(*a, **kw)
        s.reviewers.deepseek.api_key = ""
        return s

    monkeypatch.setattr(cli_mod, "load_settings", _load_no_key)

    result = runner.invoke(app, ["ping-deepseek"])
    assert result.exit_code == 1
    assert "no DeepSeek api key" in (result.output + (result.stderr or ""))


class _FakeResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Context-manager stand-in for httpx.Client returning a canned response."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, *a, **kw) -> _FakeResp:
        return self._resp


def _enable_mocked_deepseek(monkeypatch) -> None:
    """Wire a fake key so ping-deepseek reaches the mocked httpx client."""
    from catchem.settings import reload_settings

    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__ENABLED", "true")
    monkeypatch.setenv("CATCHEM_REVIEWERS__DEEPSEEK__API_KEY", "test-key-not-real")
    reload_settings()


def test_ping_deepseek_success_mocked(monkeypatch) -> None:
    """200 from the mocked client → ok line + exit 0. No live call."""
    import httpx

    _enable_mocked_deepseek(monkeypatch)
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: _FakeClient(_FakeResp(200))
    )
    result = runner.invoke(app, ["ping-deepseek"])
    assert result.exit_code == 0, result.output
    assert "ok: DeepSeek accepted credentials" in result.output


def test_ping_deepseek_failure_mocked(monkeypatch) -> None:
    """Non-200 → fail line on stderr + exit 1, error excerpt surfaced."""
    import httpx

    _enable_mocked_deepseek(monkeypatch)
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: _FakeClient(_FakeResp(401, text="invalid api key")),
    )
    result = runner.invoke(app, ["ping-deepseek"])
    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "401" in combined
    assert "invalid api key" in combined


def test_ping_deepseek_json_failure_mocked(monkeypatch) -> None:
    """--json on a failure emits the structured envelope and still exits 1."""
    import httpx

    _enable_mocked_deepseek(monkeypatch)
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: _FakeClient(_FakeResp(429, text="rate limited")),
    )
    result = runner.invoke(app, ["ping-deepseek", "--json"])
    assert result.exit_code == 1
    payload = _extract_json(result.output)
    assert payload["ok"] is False
    assert payload["status"] == 429
    assert "rate limited" in payload["error"]


def test_ping_deepseek_transport_error_mocked(monkeypatch) -> None:
    """An httpx transport error inside the client.post call → exit 1 + message."""
    import httpx

    _enable_mocked_deepseek(monkeypatch)

    class _BoomClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _BoomClient())
    result = runner.invoke(app, ["ping-deepseek"])
    assert result.exit_code == 1
    assert "transport failure" in (result.output + (result.stderr or ""))


# ── tag-by: invalid-tag error branch ───────────────────────────────────────


def test_tag_by_rejects_invalid_tag(seeded_storage) -> None:
    """A tag the storage validator rejects (whitespace) → exit 1 + message.

    Covers the ValueError branch in cli_tag_by that test_cli_tags.py skips
    (it only exercises the happy + empty paths)."""
    result = runner.invoke(app, ["tag-by", "has spaces"])
    assert result.exit_code == 1
    assert "invalid tag" in (result.output + (result.stderr or ""))


# ── watch: drive exactly one tick, then KeyboardInterrupt ───────────────────


def test_watch_single_tick_then_interrupt(seeded_storage, monkeypatch) -> None:
    """`watch` is an infinite refresh loop guarded by ``time.sleep(interval)``.

    Patch ``time.sleep`` (imported lazily inside the command) to raise
    KeyboardInterrupt on the first call, so the body runs one full tick —
    counts query, top-record render, ANSI clear — then the ^C handler prints
    "(exited)" and the command returns 0. This is the only way to unit-test the
    otherwise-blocking loop deterministically.
    """
    import time

    def _interrupt(_secs):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _interrupt)

    result = runner.invoke(app, ["watch", "--interval", "0.5", "--min-score", "0.5", "--limit", "5"])
    assert result.exit_code == 0, result.output
    # Header + counts line from the single tick, then the exit notice.
    assert "catchem watch" in result.output
    assert "records" in result.output
    assert "(exited)" in result.output
    # The 0.92 Fed record clears the 0.5 floor, so a score-prefixed row renders.
    assert "[0.92]" in result.output


def test_watch_single_tick_empty_when_floor_too_high(seeded_storage, monkeypatch) -> None:
    """One tick with an unreachable min-score → the empty-state line, exit 0."""
    import time

    def _interrupt(_secs):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _interrupt)

    result = runner.invoke(app, ["watch", "--interval", "0.5", "--min-score", "1.0"])
    assert result.exit_code == 0, result.output
    assert "no records with score" in result.output


# ── serve: mock uvicorn so no real server binds ─────────────────────────────


def test_serve_binds_and_invokes_uvicorn(monkeypatch) -> None:
    """`serve` resolves host/port, records the bind, builds the app, and hands
    off to uvicorn.run. Mock the uvicorn module (imported lazily) so nothing
    actually listens; assert the resolved port reached uvicorn.run."""
    import sys
    import types

    from catchem.settings import reload_settings

    captured: dict = {}

    fake_uvicorn = types.ModuleType("uvicorn")

    def _fake_run(app_obj, **kwargs):
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")
        captured["log_level"] = kwargs.get("log_level")
        captured["app"] = app_obj

    fake_uvicorn.run = _fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("CATCHEM_LOGGING__LEVEL", "WARN")
    reload_settings()

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9099"])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9099
    assert captured["log_level"] == "warning"
    assert captured["app"] is not None
