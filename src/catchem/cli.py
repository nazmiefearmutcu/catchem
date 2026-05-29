"""Typer-based CLI. Wraps Supervisor and a few maintenance scripts."""
# ruff: noqa: B008
#
# B008 noqa: `typer.Option(...)` calls in parameter defaults are typer's
# parameter-declaration idiom (the same role FastAPI's `Body(...)` plays).
# Moving them inside the function body would lose the CLI metadata
# (help text, short flags) that typer uses to render `--help`.

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from .bootstrap import bootstrap as bootstrap_call
from .logging import configure_logging, get_logger
from .settings import CatchemMode, load_settings, reload_settings
from .supervisor import Supervisor

app = typer.Typer(no_args_is_help=True, help="catchem: finance-relevance layer over Awareness.")
logger = get_logger("catchem.cli")


def _override_mode(mode: str | None) -> None:
    if mode is not None:
        import os
        os.environ["CATCHEM_MODE"] = mode
        reload_settings()


@app.command()
def run(
    mode: str = typer.Option("replay_existing", help="production_safe | replay_existing | live_tail | research_diagnostic"),
    max_records: int | None = typer.Option(None, help="Replay limit (replay mode only)."),
) -> None:
    """Run the pipeline in one of the supported modes."""
    _override_mode(mode)
    s = load_settings()
    sup = Supervisor(s)
    try:
        if s.mode == CatchemMode.LIVE_TAIL:
            sup.run_tail()
        else:
            counts = sup.run_replay(max_records=max_records)
            typer.echo(json.dumps(counts))
    finally:
        sup.close()


@app.command()
def replay(
    path: Path | None = typer.Option(None, help="Path to a JSONL file to replay (overrides discovery)."),
    max_records: int | None = typer.Option(None),
) -> None:
    """Replay a single JSONL file or the default Awareness root."""
    s = load_settings()
    sup = Supervisor(s)
    try:
        # `--path FILE` honors the single-file contract: only that exact file
        # is replayed. We pass it straight through to run_replay rather than
        # redirecting the data dir at its parent (which swept in every sibling
        # *.jsonl under the directory — the opposite of "single file").
        counts = sup.run_replay(max_records=max_records, file=path if path else None)
        typer.echo(json.dumps(counts))
    finally:
        sup.close()


@app.command()
def inspect(capture_id: str = typer.Option(..., "--capture-id")) -> None:
    """Print a single FinancialImpactRecord by capture_id."""
    s = load_settings()
    sup = Supervisor(s)
    try:
        rec = sup.storage.get_record(capture_id)
        if rec is None:
            typer.echo(f"no record for {capture_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(rec, indent=2, default=str))
    finally:
        sup.close()


@app.command()
def benchmark(
    limit: int = typer.Option(200, help="Replay record cap for throughput test."),
    golden: bool = typer.Option(False, "--golden", help="Run the curated golden-set evaluation instead of throughput."),
    extended: Path | None = typer.Option(None, help="Optional path to a JSONL of extra golden items."),
) -> None:
    """Throughput sanity check OR golden-set precision/recall/F1."""
    import time

    s = load_settings()
    configure_logging(level=s.logging.level, json_mode=False)
    if golden:
        from .golden import SYNTHETIC, load_extended, run_benchmark
        from .service import build_service

        svc = build_service(s)
        items = list(SYNTHETIC)
        if extended is not None:
            items.extend(load_extended(extended))
        rep = run_benchmark(svc, items)
        typer.echo(json.dumps(rep.to_dict(), indent=2))
        return

    sup = Supervisor(s)
    try:
        t0 = time.time()
        counts = sup.run_replay(max_records=limit)
        elapsed = time.time() - t0
        rate = counts["processed"] / max(elapsed, 1e-9)
        typer.echo(json.dumps({**counts, "elapsed_s": round(elapsed, 3), "rps": round(rate, 1)}))
    finally:
        sup.close()


@app.command("validate-guards")
def validate_guards() -> None:
    """Run the NewsImpact guard verifier without starting the pipeline."""
    import subprocess

    s = load_settings()
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_newsimpact_guard.py"
    res = subprocess.run([sys.executable, str(script), str(s.paths.newsimpact_repo)])
    raise typer.Exit(res.returncode)


# NOTE: bound to the distinct name "status-json" (not "status"). The later
# `cli_status` below also wanted the "status" name for its one-line health
# summary, and Typer keeps only the LAST registration for a given name — so
# leaving this as `@app.command()` (auto-named "status") made it dead code,
# permanently shadowed and unreachable from the CLI. The two outputs are NOT
# redundant: this dumps Supervisor.status() (mode, diagnostic_enabled,
# use_ml_stubs, dlq count, model_versions, reviewer status) which no other
# command surfaces, so it's preserved under its own name rather than deleted.
@app.command("status-json")
def status() -> None:
    """Print full supervisor status as JSON (mode, models, DLQ, reviewers)."""
    s = load_settings()
    sup = Supervisor(s)
    try:
        typer.echo(json.dumps(sup.status(), indent=2))
    finally:
        sup.close()


@app.command()
def serve(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """Start the FastAPI HTTP server."""
    import uvicorn

    s = load_settings()
    from .api import create_app, record_bind

    # Resolve the actual host/port FIRST, then record it so /ui/sidecar-status
    # reports the bind truth rather than the static settings value. Without
    # this, `--port 9090` still surfaces as `:8087` in the Tauri shell.
    bind_host = host or s.api.host
    bind_port = int(port or s.api.port)
    record_bind(bind_host, bind_port)

    # uvicorn accepts only a fixed set of level names — it rejects stdlib
    # aliases like WARN / NOTSET that are perfectly valid in LoggingConfig and
    # would otherwise crash serve() before it ever binds the port. Normalize to
    # uvicorn's vocabulary, falling back to "info" for anything unrecognized.
    _UVICORN_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}
    lvl = s.logging.level.strip().lower()
    if lvl in ("warn",):
        lvl = "warning"
    if lvl not in _UVICORN_LEVELS:
        lvl = "info"

    app_ = create_app(s)
    uvicorn.run(
        app_,
        host=bind_host,
        port=bind_port,
        log_level=lvl,
    )


@app.command("bootstrap-init")
def bootstrap_init(skip_warm: bool = typer.Option(True, help="Skip HF model warm-cache.")) -> None:
    """Initialize catchem directories and verify guard. Idempotent."""
    summary = bootstrap_call(skip_warm=skip_warm)
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def demo(
    title: str = typer.Option(..., "--title", "-t", help="Headline of the article to ingest."),
    text: str | None = typer.Option(None, "--text", help="Article body. If omitted, read from --text-file or stdin."),
    text_file: Path | None = typer.Option(None, "--text-file", help="Read article body from this file."),
    domain: str = typer.Option("demo.local", "--domain", help="Source domain (sets the domain prior)."),
    url: str | None = typer.Option(None, "--url", help="Canonical URL (sanity-checked by the safeHref filter in UI)."),
    json_out: bool = typer.Option(False, "--json", help="Emit the raw record JSON instead of the report."),
) -> None:
    """End-to-end demo: paste one news article, write the same JSONL Awareness
    would write, run the replay, and print the materialized record.

    Examples:
      catchem demo --title "Fed raises rates by 25 bps" --text "The Fed hiked..."
      catchem demo -t "Apple beats earnings" --text-file body.txt --domain wsj.com
      cat body.txt | catchem demo -t "Headline" --domain reuters.com
    """
    from .demo import render_demo_report, run_demo

    if text is None and text_file is not None:
        text = text_file.read_text(encoding="utf-8")
    if text is None and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text or not text.strip():
        typer.echo("error: provide --text, --text-file, or pipe stdin", err=True)
        raise typer.Exit(2)

    configure_logging(level="WARNING", json_mode=False)
    result = run_demo(title=title, text=text.strip(), domain=domain, url=url)
    if json_out:
        typer.echo(json.dumps(result.record, indent=2, default=str))
    else:
        typer.echo(render_demo_report(result))


# ──────────────────────────────────────────────────────────────────────────────
# Analyst utility subcommands.
#
# All of these operate DIRECTLY on Storage (or the quant engine bound to it).
# They do NOT spin up the FastAPI sidecar, the news poller, or the archive
# drainer — Supervisor's network-facing components stay dormant.
# Plain `typer.echo()` output by default; pass `--json` where appropriate for
# machine-readable shape.
# ──────────────────────────────────────────────────────────────────────────────


def _open_storage_readonly():
    """Build a Storage that points at the configured SQLite path.

    `Storage.__init__` creates the parent dirs and runs IF-NOT-EXISTS DDL,
    which is harmless against an existing DB. For sub-commands that need
    rich data (search, export, quant-snapshot) we wrap this through a
    Supervisor so the QuantEngine / market-data adapters are available.
    Sub-commands that only need DB stats use the bare Storage directly.
    """
    from .storage import load_storage_from_settings

    settings = load_settings()
    return settings, load_storage_from_settings(settings)


def _format_size(n: int) -> str:
    """Human-readable byte count for db-info / db-backup output."""
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} {unit}"
        size /= 1024.0
    return f"{n} B"


@app.command("db-info")
def cli_db_info(json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text.")) -> None:
    """Print SQLite path, size, schema version, and last modified time."""
    import sqlite3

    settings = load_settings()
    db_path = settings.sqlite_path()
    if not db_path.exists():
        typer.echo(f"error: db not found at {db_path}", err=True)
        raise typer.Exit(1)

    stat = db_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()

    from .migrations import current_version, max_known_version

    with sqlite3.connect(db_path) as conn:
        applied = current_version(conn)
        try:
            total = int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            relevant = int(
                conn.execute("SELECT COUNT(*) FROM records WHERE is_finance_relevant = 1").fetchone()[0]
            )
        except sqlite3.OperationalError:
            total, relevant = 0, 0
    target = max_known_version()

    if json_out:
        typer.echo(json.dumps({
            "path": str(db_path),
            "size_bytes": stat.st_size,
            "modified": modified,
            "schema_version": applied,
            "schema_target": target,
            "records_total": total,
            "records_relevant": relevant,
        }, indent=2))
        return

    typer.echo(f"Path:           {db_path}")
    typer.echo(f"Size:           {_format_size(stat.st_size)} ({stat.st_size:,} bytes)")
    typer.echo(f"Modified:       {modified}")
    typer.echo(f"Schema version: {applied} / {target}")
    typer.echo(f"Records:        {total:,} total, {relevant:,} finance-relevant")


@app.command("db-backup")
def cli_db_backup(
    output: Path | None = typer.Option(None, "--output", "-o", help="Backup file path (defaults to ./catchem-backup-<timestamp>.sqlite3)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Copy the SQLite database to a backup file (uses sqlite3 backup API)."""
    import sqlite3

    settings = load_settings()
    src = settings.sqlite_path()
    if not src.exists():
        typer.echo(f"error: db not found at {src}", err=True)
        raise typer.Exit(1)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = output if output is not None else Path.cwd() / f"catchem-backup-{stamp}.sqlite3"
    target = target.expanduser().resolve()
    if target.is_dir():
        target = target / f"catchem-backup-{stamp}.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)

    # Use the sqlite3 backup API rather than a raw file copy so the WAL
    # is correctly drained — a plain shutil.copy could miss in-flight pages.
    try:
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(target)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
    except sqlite3.Error as exc:
        typer.echo(f"error: backup failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    size = target.stat().st_size
    if json_out:
        typer.echo(json.dumps({
            "source": str(src),
            "backup": str(target),
            "size_bytes": size,
        }, indent=2))
        return

    typer.echo(f"Source: {src}")
    typer.echo(f"Backup: {target}")
    typer.echo(f"Size:   {_format_size(size)} ({size:,} bytes)")


@app.command("bench")
def cli_bench(
    extended: Path | None = typer.Option(None, "--extended", help="Optional JSONL of extra golden items."),
    json_out: bool = typer.Option(False, "--json", help="Emit the full benchmark report JSON."),
) -> None:
    """Run the curated golden-set benchmark and print precision / recall / F1."""
    from .golden import SYNTHETIC, load_extended, run_benchmark
    from .service import build_service

    settings = load_settings()
    configure_logging(level="WARNING", json_mode=False)
    items = list(SYNTHETIC)
    if extended is not None:
        items.extend(load_extended(extended))

    try:
        svc = build_service(settings)
        rep = run_benchmark(svc, items)
    except Exception as exc:
        typer.echo(f"error: benchmark failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = rep.to_dict()
    if json_out:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    relevance = payload["relevance"]
    typer.echo(f"Dataset:        {payload['dataset_name']} (n={payload['n']})")
    typer.echo(f"Relevance P/R/F1: {relevance['precision']:.3f} / {relevance['recall']:.3f} / {relevance['f1']:.3f}")
    sym = payload.get("symbol_recall")
    if sym is not None:
        typer.echo(f"Symbol recall:  {sym:.3f}")
    sent = payload.get("sentiment_accuracy")
    if sent is not None:
        typer.echo(f"Sentiment acc:  {sent:.3f}")
    # Per-label highlights (top 5 by F1) keep the text output tight.
    for kind in ("asset_class_f1", "reason_code_f1"):
        bucket = payload.get(kind) or {}
        if not bucket:
            continue
        rows = sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:5]
        typer.echo(f"Top {kind}:")
        for k, v in rows:
            typer.echo(f"  {k:<24} F1={v:.3f}")


@app.command("search")
def cli_search(
    query: str = typer.Argument(..., help="Free-text query (matches title, domain, ticker, cluster_id)."),
    limit: int = typer.Option(20, "--limit", "-l", help="Max rows per category."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of formatted text."),
) -> None:
    """Search records, symbols, and clusters by free-text query."""
    from collections import Counter

    settings, _storage = _open_storage_readonly()
    q_lower = query.strip().lower()
    if not q_lower:
        typer.echo("error: query must not be empty", err=True)
        raise typer.Exit(1)

    sup = Supervisor(settings)
    try:
        records_raw = sup.storage.recent_records(limit=500, relevant_only=False)
    finally:
        sup.close()

    matched_records: list[dict] = []
    for r in records_raw:
        title = (r.get("title") or "").lower()
        domain = (r.get("domain") or "").lower()
        if q_lower in title or q_lower in domain:
            matched_records.append({
                "capture_id": r.get("capture_id"),
                "title": r.get("title"),
                "domain": r.get("domain"),
                "score": float(r.get("finance_relevance_score") or 0.0),
                "published_ts": r.get("published_ts"),
            })
            if len(matched_records) >= limit:
                break

    sym_counter: Counter[str] = Counter()
    for r in records_raw:
        for s in r.get("candidate_symbols", []) or []:
            sym_counter[s] += 1
    matched_symbols = [
        {"symbol": sym, "count": int(count)}
        for sym, count in sym_counter.most_common(200)
        if q_lower in sym.lower()
    ][:limit]

    # Cluster search runs through the QuantEngine which needs an active
    # storage handle. Build a fresh Supervisor for the engine, then close.
    matched_clusters: list[dict] = []
    sup2 = Supervisor(settings)
    try:
        from .quant import QuantEngine

        engine = QuantEngine(storage=sup2.storage)
        try:
            cs = engine.clusters(limit=2000)
        except Exception:
            cs = []
        for c in cs[:200]:
            cid = (c.cluster_id or "").lower()
            symbols = tuple(c.dominant_symbols or ())
            if q_lower in cid or any(q_lower in (s or "").lower() for s in symbols):
                matched_clusters.append({
                    "cluster_id": c.cluster_id,
                    "size": int(c.size),
                    "symbols": list(symbols),
                })
                if len(matched_clusters) >= limit:
                    break
    finally:
        sup2.close()

    if json_out:
        typer.echo(json.dumps({
            "query": query,
            "records": matched_records,
            "symbols": matched_symbols,
            "clusters": matched_clusters,
        }, indent=2, default=str))
        return

    typer.echo(f"Records ({len(matched_records)}):")
    for r in matched_records:
        title = (r.get("title") or "(untitled)")[:80]
        typer.echo(f"  [{r['score']:.2f}] {title}  —  {r.get('domain') or '(no domain)'}")
    if not matched_records:
        typer.echo("  (no matches)")
    typer.echo("")
    typer.echo(f"Symbols ({len(matched_symbols)}):")
    for s in matched_symbols:
        typer.echo(f"  {s['symbol']:<12} ({s['count']} mentions)")
    if not matched_symbols:
        typer.echo("  (no matches)")
    typer.echo("")
    typer.echo(f"Clusters ({len(matched_clusters)}):")
    for c in matched_clusters:
        syms = ",".join(c["symbols"][:4]) or "(no symbols)"
        typer.echo(f"  {c['cluster_id']}  size={c['size']:<3}  symbols={syms}")
    if not matched_clusters:
        typer.echo("  (no matches)")


@app.command("export")
def cli_export(
    fmt: str = typer.Argument(..., help="Output format: csv | json"),
    asset_class: str | None = typer.Option(None, "--asset-class", help="Filter by asset class label."),
    reason_code: str | None = typer.Option(None, "--reason-code", help="Filter by impact reason code."),
    symbol: str | None = typer.Option(None, "--symbol", help="Filter by candidate symbol."),
    min_score: float | None = typer.Option(None, "--min-score", help="Drop records with score below this floor."),
    limit: int = typer.Option(5000, "--limit", help="Cap on rows read from storage."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file path (default: ./catchem-records-<ts>.<ext>)."),
) -> None:
    """Filtered CSV/JSON export — mirrors /api/export/records to disk."""
    fmt_lower = fmt.strip().lower()
    if fmt_lower not in ("csv", "json"):
        typer.echo(f"error: format must be csv or json, got {fmt!r}", err=True)
        raise typer.Exit(1)

    settings = load_settings()
    sup = Supervisor(settings)
    try:
        raw = sup.storage.recent_records(limit=limit, relevant_only=False)
    finally:
        sup.close()

    # Reuse the same redaction + filter helpers the HTTP route uses so the
    # CLI's output is byte-for-byte the same as a curl to /api/export/records.
    from .redaction import redact_records_for_mode

    records = redact_records_for_mode(raw, production_safe=settings.is_production_safe())
    if asset_class:
        records = [r for r in records if asset_class in (r.get("asset_classes") or [])]
    if reason_code:
        records = [r for r in records if reason_code in (r.get("impact_reason_codes") or [])]
    if symbol:
        records = [r for r in records if symbol in (r.get("candidate_symbols") or [])]
    if min_score is not None:
        records = [r for r in records if (r.get("finance_relevance_score") or 0.0) >= min_score]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = output if output is not None else Path.cwd() / f"catchem-records-{stamp}.{fmt_lower}"
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if fmt_lower == "json":
        payload = {
            "exported_at": datetime.now(UTC).isoformat(),
            "count": len(records),
            "filters": {
                "asset_class": asset_class,
                "reason_code": reason_code,
                "symbol": symbol,
                "min_score": min_score,
                "limit": limit,
            },
            "items": records,
        }
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    else:
        import csv
        from io import StringIO

        # Same CSV formula-injection defense the HTTP twin (/api/export/records)
        # and the drive archiver apply: feed-sourced text (title/domain/url +
        # the joined label lists) must be neutralized so a hostile RSS headline
        # like `=HYPERLINK(...)` / `+cmd|'/C calc'!A0` can't execute when the
        # analyst opens the export in a spreadsheet (CWE-1236).
        from .archive import _csv_safe

        fields = (
            "capture_id", "title", "domain", "url", "published_ts", "created_at",
            "is_finance_relevant", "finance_relevance_score",
            "sentiment_label", "sentiment_score",
            "asset_classes", "impact_reason_codes", "candidate_symbols",
            "processing_mode",
        )
        _text_fields = ("title", "domain", "url", "asset_classes", "impact_reason_codes", "candidate_symbols")
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = {k: r.get(k) for k in fields}
            for list_field in ("asset_classes", "impact_reason_codes", "candidate_symbols"):
                v = row.get(list_field)
                if isinstance(v, list):
                    row[list_field] = ";".join(str(x) for x in v)
            for tf in _text_fields:
                v = row.get(tf)
                if isinstance(v, str):
                    row[tf] = _csv_safe(v)
            writer.writerow(row)
        target.write_text(buf.getvalue(), encoding="utf-8")

    typer.echo(f"Wrote {len(records):,} record(s) to {target}")


@app.command("quant-snapshot")
def cli_quant_snapshot(
    limit: int = typer.Option(1000, "--limit", help="Records pulled into the snapshot."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Optional file path; default = stdout."),
) -> None:
    """Run the quant engine and dump the dashboard snapshot as JSON."""
    settings = load_settings()
    sup = Supervisor(settings)
    try:
        from .quant import QuantEngine

        engine = QuantEngine(storage=sup.storage)
        snap = engine.dashboard_snapshot(limit=limit)
    finally:
        sup.close()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "limit": limit,
        "snapshot": snap,
    }
    text = json.dumps(payload, indent=2, default=str)
    if output is None:
        typer.echo(text)
    else:
        target = output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote quant snapshot to {target}")


# ──────────────────────────────────────────────────────────────────────────────
# Tag CRUD subcommands (mirrors /api/records/{id}/tags + /api/tags from v38).
#
# Direct Storage open — no Supervisor / sidecar required. Output is plain text
# (no --json flag in v1 to keep scope tight; pipe through `catchem search` or
# `catchem export` if structured shape is needed).
# ──────────────────────────────────────────────────────────────────────────────


def _open_storage():
    """Open Storage directly without spinning up the Supervisor.

    The Supervisor pulls in the QuantEngine, market-data adapters, news poller,
    and archive drainer — none of which are needed for tag CRUD. Going through
    ``load_storage_from_settings`` shaves ~300ms of startup off each invocation.
    """
    from .storage import load_storage_from_settings

    settings = load_settings()
    return load_storage_from_settings(settings)


@app.command("tag-add")
def cli_tag_add(capture_id: str, tag: str) -> None:
    """Add a tag to a record."""
    storage = _open_storage()
    try:
        try:
            added = storage.add_record_tag(capture_id, tag)
        except ValueError as exc:
            typer.echo(f"error: invalid tag: {exc}", err=True)
            raise typer.Exit(1) from exc
        marker = "+" if added else "="
        state = "added" if added else "already present"
        typer.echo(f"{marker} {capture_id} -> {tag!r} ({state})")
    finally:
        storage.close()  # v66 audit fix: drain WAL + parquet flush even on exit


@app.command("tag-remove")
def cli_tag_remove(capture_id: str, tag: str) -> None:
    """Remove a tag from a record."""
    storage = _open_storage()
    try:
        try:
            removed = storage.remove_record_tag(capture_id, tag)
        except ValueError as exc:
            typer.echo(f"error: invalid tag: {exc}", err=True)
            raise typer.Exit(1) from exc
        marker = "-" if removed else "="
        state = "removed" if removed else "was not present"
        typer.echo(f"{marker} {capture_id} -> {tag!r} ({state})")
    finally:
        storage.close()


@app.command("tag-list")
def cli_tag_list(capture_id: str) -> None:
    """List tags on a record."""
    storage = _open_storage()
    try:
        tags = storage.get_record_tags(capture_id)
        if not tags:
            typer.echo(f"(no tags on {capture_id})")
            return
        typer.echo(f"{capture_id}:")
        for t in tags:
            typer.echo(f"  - {t}")
    finally:
        storage.close()


@app.command("tag-top")
def cli_tag_top(
    limit: int = typer.Option(20, "--limit", "-l", help="Max tags to return."),
) -> None:
    """List most-used tags (count desc)."""
    storage = _open_storage()
    try:
        rows = storage.top_tags(limit=limit)
        if not rows:
            typer.echo("(no tags yet)")
            return
        for row in rows:
            typer.echo(f"  {int(row['count']):>5}  {row['tag']}")
    finally:
        storage.close()


@app.command("tag-by")
def cli_tag_by(
    tag: str,
    limit: int = typer.Option(20, "--limit", "-l", help="Max records to return."),
) -> None:
    """List records carrying a specific tag."""
    storage = _open_storage()
    try:
        try:
            records = storage.records_by_tag(tag, limit=limit)
        except ValueError as exc:
            typer.echo(f"error: invalid tag: {exc}", err=True)
            raise typer.Exit(1) from exc
        if not records:
            typer.echo(f"(no records with tag {tag!r})")
            return
        for r in records:
            title = (r.get("title") or "(no title)")[:80]
            domain = r.get("domain") or "?"
            score = r.get("finance_relevance_score")
            score_str = f"{float(score):.2f}" if score is not None else "  - "
            cid = (r.get("capture_id") or "")[:8]
            typer.echo(f"  [{score_str}] {cid}...  {title}  - {domain}")
    finally:
        storage.close()


# ──────────────────────────────────────────────────────────────────────────────
# Portfolio command group (mirrors /api/portfolio + /api/portfolio/enriched).
#
# READ-ONLY holdings tracker — NO order execution, NO money movement. Mounted
# as a Typer sub-app so the surface reads as `catchem portfolio <verb>`, the
# same verb-per-subcommand shape as the HTTP routes. All verbs open Storage
# directly (no Supervisor / sidecar): list/add/remove are plain CRUD, and
# `show` runs the pure `enrich_holdings` join over recent records + the
# fixture market provider's quote — identical to /api/portfolio/enriched but
# offline.
# ──────────────────────────────────────────────────────────────────────────────

portfolio_app = typer.Typer(
    no_args_is_help=False,
    help="Read-only holdings tracker (mirrors /api/portfolio). No order execution.",
)
app.add_typer(portfolio_app, name="portfolio")


def _print_holdings_text(holdings: list[dict]) -> None:
    """Tabular text render shared by ``portfolio list`` (default + explicit)."""
    if not holdings:
        typer.echo("(no holdings — add one with: catchem portfolio add SYMBOL)")
        return
    for h in holdings:
        hid = h.get("id")
        sym = h.get("symbol") or "?"
        label = h.get("label") or "—"
        shares = h.get("shares")
        shares_str = f"{float(shares):g}" if shares is not None else "—"
        cost = h.get("cost_basis")
        cost_str = f"{float(cost):g}" if cost is not None else "—"
        added = (h.get("added_at") or "")[:19] or "?"
        typer.echo(f"  [{hid}] {sym:<10} {label:<16} shares={shares_str:<10} cost={cost_str:<10} added={added}")


@portfolio_app.callback(invoke_without_command=True)
def portfolio_main(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Default action (no subcommand) = ``portfolio list``.

    Mirrors the HTTP root GET /api/portfolio. With a subcommand given, this
    callback is a no-op and the subcommand runs as usual.
    """
    if ctx.invoked_subcommand is not None:
        return
    _portfolio_list_impl(json_out)


def _portfolio_list_impl(json_out: bool) -> None:
    storage = _open_storage()
    try:
        holdings = storage.list_holdings()
    finally:
        storage.close()
    if json_out:
        typer.echo(json.dumps({
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "holdings": holdings,
        }, default=str))
        return
    _print_holdings_text(holdings)


@portfolio_app.command("list")
def cli_portfolio_list(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
    enriched: bool = typer.Option(
        False, "--enriched",
        help="Run the awareness + quote join (same as `portfolio show`).",
    ),
) -> None:
    """List holdings (symbol, label, shares, cost_basis, added_at)."""
    if enriched:
        _portfolio_show_impl(json_out, window_seconds=86400.0, record_limit=500)
        return
    _portfolio_list_impl(json_out)


@portfolio_app.command("add")
def cli_portfolio_add(
    symbol: str = typer.Argument(..., help="Ticker / symbol to track (required)."),
    shares: float | None = typer.Option(None, "--shares", help="Share / unit count."),
    label: str | None = typer.Option(None, "--label", help="Human-readable label."),
    cost_basis: float | None = typer.Option(None, "--cost-basis", help="Per-position cost basis."),
    weight: float | None = typer.Option(None, "--weight", help="Optional portfolio weight."),
    notes: str | None = typer.Option(None, "--notes", help="Free-text notes."),
    json_out: bool = typer.Option(False, "--json", help="Emit the created holding as JSON."),
) -> None:
    """Add a holding — mirrors POST /api/portfolio."""
    storage = _open_storage()
    try:
        try:
            holding = storage.add_holding(
                symbol,
                label=label,
                shares=shares,
                weight=weight,
                cost_basis=cost_basis,
                notes=notes,
            )
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    finally:
        storage.close()

    if json_out:
        typer.echo(json.dumps(holding, default=str))
        return
    typer.echo(f"+ [{holding['id']}] {holding['symbol']} added")
    _print_holdings_text([holding])


@portfolio_app.command("remove")
def cli_portfolio_remove(
    holding_id: int = typer.Argument(..., help="Numeric id of the holding to delete."),
) -> None:
    """Delete a holding by id — mirrors DELETE /api/portfolio/{id}. Exit 1 if absent."""
    storage = _open_storage()
    try:
        removed = storage.delete_holding(holding_id)
    finally:
        storage.close()
    if not removed:
        typer.echo(f"error: no holding with id {holding_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"- holding {holding_id} removed")


def _portfolio_show_impl(json_out: bool, *, window_seconds: float, record_limit: int) -> None:
    """Shared body for ``portfolio show`` and ``portfolio list --enriched``.

    Runs the pure :func:`catchem.portfolio.enrich_holdings` over recent records
    with the fixture market-data quote fn — the offline twin of
    /api/portfolio/enriched. Records may be empty (no sidecar / fresh DB);
    enrichment then returns each holding with empty news + None quote, still
    exit 0.
    """
    from .market_data import LocalFixtureMarketDataProvider
    from .portfolio import enrich_holdings

    storage = _open_storage()
    try:
        holdings = storage.list_holdings()
        records = storage.recent_records(limit=record_limit, relevant_only=False)
    finally:
        storage.close()

    provider = LocalFixtureMarketDataProvider()
    enriched = enrich_holdings(
        holdings,
        records=records,
        quote_fn=provider.quote,
        now=datetime.now(UTC),
        window_seconds=window_seconds,
    )

    if json_out:
        typer.echo(json.dumps({
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "holdings": enriched,
        }, default=str))
        return

    if not enriched:
        typer.echo("(no holdings — add one with: catchem portfolio add SYMBOL)")
        return
    for h in enriched:
        sym = h.get("symbol") or "?"
        quote = h.get("quote") or {}
        last = quote.get("last")
        change_pct = quote.get("change_pct")
        if last is not None:
            chg = f" ({change_pct * 100:+.2f}%)" if change_pct is not None else ""
            price_str = f"{float(last):g}{chg}"
        else:
            price_str = "no quote"
        cov = h.get("coverage") or {}
        cov_str = "covered" if cov.get("covered") else "BLIND SPOT"
        news_n = int(h.get("recent_news_count") or 0)
        top = h.get("recent_top") or []
        headline = (top[0].get("title") if top and top[0].get("title") else "—")
        if headline and len(headline) > 60:
            headline = headline[:57] + "..."
        typer.echo(
            f"  [{h.get('id')}] {sym:<10} {price_str:<22} {cov_str:<11} "
            f"news={news_n:<3} top={headline}"
        )


@portfolio_app.command("show")
def cli_portfolio_show(
    window_seconds: float = typer.Option(
        86400.0, "--window-seconds", "-w", min=1.0,
        help="Coverage / news-count horizon in seconds (default 24h).",
    ),
    record_limit: int = typer.Option(
        500, "--record-limit", "-l", min=1, max=5000,
        help="Records pulled from storage for the enrichment join.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Holdings joined to recent news + coverage + a quote — offline twin of
    /api/portfolio/enriched. Per holding: latest quote (last + change_pct),
    coverage (covered / blind-spot), recent_news_count, and top headline.
    """
    _portfolio_show_impl(json_out, window_seconds=window_seconds, record_limit=record_limit)


@app.command("watch")
def cli_watch(
    interval: float = typer.Option(3.0, "--interval", "-n", min=0.5, max=60.0, help="Refresh cadence in seconds."),
    limit: int = typer.Option(8, "--limit", "-l", help="Number of top-recent rows to show."),
    min_score: float = typer.Option(0.5, "--min-score", "-m", help="Minimum finance score."),
) -> None:
    """Continuous-refresh dashboard in the terminal — top-recent + status.

    Like `watch -n 3 catchem top-recent` but built-in: opens Storage once
    (no per-tick startup overhead), clears the screen each tick, prints
    status one-liner + top scoring records. ^C to exit.

    Sub-second refresh interval is rejected — SQLite COUNT(*) per tick adds
    up under abuse. 0.5s floor matches the rate-limit bucket cadence.
    """
    import time

    from .migrations import current_version

    storage = _open_storage()
    try:
        tick_n = 0
        while True:
            tick_n += 1
            # Fetch fresh data each tick
            with storage._lock, storage._connection() as conn:
                rec_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                try:
                    tag_count = conn.execute("SELECT COUNT(*) FROM record_tags").fetchone()[0]
                except Exception:
                    tag_count = 0
                schema = current_version(conn)
            records = storage.recent_records(200, relevant_only=False)
            filtered = [r for r in records if (r.get("finance_relevance_score") or 0.0) >= min_score]
            filtered.sort(key=lambda r: float(r.get("finance_relevance_score") or 0.0), reverse=True)
            top = filtered[:limit]

            # ANSI clear-screen + cursor-home
            typer.echo("\033[2J\033[H", nl=False)
            now = datetime.now(UTC).strftime("%H:%M:%S UTC")
            typer.echo(f"catchem watch · tick {tick_n} · {now}  (^C to exit)")
            typer.echo(
                f"[catchem v{schema}] {rec_count:,} records · {tag_count:,} tags · "
                f"min score {min_score:.2f} · refresh {interval:.1f}s\n"
            )
            if not top:
                typer.echo(f"  (no records with score ≥ {min_score:.2f})")
            else:
                for r in top:
                    score = r.get("finance_relevance_score") or 0.0
                    title = (r.get("title") or "(no title)")[:90]
                    domain = r.get("domain") or "?"
                    sent = (r.get("sentiment_label") or "—")[:8]
                    syms = ",".join((r.get("candidate_symbols") or [])[:2]) or "—"
                    typer.echo(f"  [{score:.2f}] {sent:<8}  {title}  · {domain}  · {syms}")
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\n(exited)")
    finally:
        storage.close()


@app.command("db-stats")
def cli_db_stats(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Per-table SQLite row counts + index summary + page-count size.

    Sidecar not required — opens Storage directly. Useful for spotting
    unexpected table growth (e.g., dlq mounting up) or verifying tag table
    presence post-v38 migration.
    """
    storage = _open_storage()
    try:
        with storage._lock, storage._connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = []
            for r in rows:
                # Quote identifier defensively — same rationale as api.py
                # db_stats: SQLite can't parameterize names but does honor
                # double-quotes; strip embedded `"` first.
                safe_name = r[0].replace('"', '')
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0]
                except Exception:
                    n = -1
                tables.append({"name": r[0], "rows": int(n)})
            idx_rows = conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name, name"
            ).fetchall()
            indexes = [{"name": r[0], "table": r[1]} for r in idx_rows]
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    finally:
        storage.close()

    size_bytes = page_count * page_size
    if json_out:
        typer.echo(json.dumps({
            "schema_version": 1,
            "tables": tables,
            "indexes": indexes,
            "total_tables": len(tables),
            "total_indexes": len(indexes),
            "page_count": page_count,
            "page_size_bytes": page_size,
            "estimated_size_bytes": size_bytes,
        }))
        return

    typer.echo(f"catchem db-stats · {len(tables)} tables · {len(indexes)} indexes · {size_bytes / 1024 / 1024:.1f} MB on disk\n")
    typer.echo("tables:")
    for t in tables:
        rows_str = f"{t['rows']:,}" if t['rows'] >= 0 else "ERR"
        typer.echo(f"  {rows_str:>10}  {t['name']}")
    if indexes:
        typer.echo("\nindexes:")
        for ix in indexes:
            typer.echo(f"  {ix['table']:<20}  {ix['name']}")


def _signals_diagnostics(json_out: bool) -> None:
    """Implementation of ``catchem signals --diagnostics``.

    Talks to the running sidecar's /api/quant/diagnostics endpoint. Why
    HTTP and not a direct ``QuantEngine.diagnostics()`` call? The failure
    ring buffer lives in *that* process — it's the running supervisor's
    memory. Spinning up a fresh engine here would always report zero
    failures because we'd be looking at a different buffer. Same reason
    ``catchem status`` and ``catchem watch`` use HTTP.

    Resolves host/port via ``settings.api.host/port`` (NOT the legacy
    top-level fields — v66 audit fix). Short timeout + clean error
    message when the sidecar isn't running so the CLI fails fast.
    """
    import httpx

    settings = load_settings()
    host = settings.api.host or "127.0.0.1"
    port = settings.api.port or 8087
    url = f"http://{host}:{port}/api/quant/diagnostics"
    try:
        resp = httpx.get(url, timeout=2.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"sidecar unreachable at {host}:{port} ({exc})"
        if json_out:
            typer.echo(json.dumps({"ok": False, "error": msg}))
        else:
            typer.secho(msg, err=True, fg=typer.colors.RED)
            typer.echo("  start it with: catchem serve")
        raise typer.Exit(code=2) from exc

    payload = resp.json()
    if json_out:
        typer.echo(json.dumps(payload))
        return

    total = int(payload.get("total_failures") or 0)
    capacity = int(payload.get("buffer_capacity") or 50)
    per_signal: dict[str, int] = payload.get("per_signal") or {}
    recent: list[dict] = payload.get("recent") or []

    if total == 0:
        typer.echo("all signals nominal — failure buffer empty")
        typer.echo(f"  (capacity: {capacity}, last check: {payload.get('generated_at', '?')})")
        return

    typer.secho(
        f"{total} signal failure(s) in last {capacity} (newest first)",
        fg=typer.colors.YELLOW,
    )
    typer.echo("\nper-signal counts:")
    for sig, count in sorted(per_signal.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {sig:<22}  {count:>3} failure(s)")
    typer.echo("\nmost recent (top 5):")
    for entry in recent[:5]:
        ts = entry.get("ts", "?")
        sig = entry.get("signal", "?")
        cls = entry.get("error_class", "?")
        err = entry.get("error", "")[:80]
        elapsed = entry.get("elapsed_ms", 0.0)
        typer.echo(f"  [{ts}] {sig} · {cls} ({elapsed:.1f}ms)")
        typer.echo(f"    └─ {err}")


def _awareness_live(json_out: bool) -> None:
    """Implementation of ``catchem awareness --live``.

    Queries the running sidecar's /api/news/awareness endpoint — the live
    awareness window (poll cadence + publisher lag + breadth) lives in
    *that* process's NewsPoller, so a fresh import here would always report
    zero lag / zero ingested. Same rationale as ``signals --diagnostics``.

    Resolves host/port via ``settings.api.host/port`` (the v66 audit shape,
    NOT the legacy top-level fields). Short timeout + actionable message +
    exit code 2 when the sidecar isn't running.
    """
    import httpx

    settings = load_settings()
    host = settings.api.host or "127.0.0.1"
    port = settings.api.port or 8087
    url = f"http://{host}:{port}/api/news/awareness"
    try:
        resp = httpx.get(url, timeout=2.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"sidecar unreachable at {host}:{port} ({exc})"
        if json_out:
            typer.echo(json.dumps({"ok": False, "error": msg}))
        else:
            typer.secho(msg, err=True, fg=typer.colors.RED)
            typer.echo("  start it with: catchem serve")
        raise typer.Exit(code=2) from exc

    payload = resp.json()
    if json_out:
        typer.echo(json.dumps(payload))
        return

    total = int(payload.get("sources_total") or 0)
    by_parser: dict[str, int] = payload.get("sources_by_parser") or {}
    interval = payload.get("poll_interval_seconds")
    median_lag = payload.get("median_publisher_lag_seconds")
    window = payload.get("window_estimate_seconds")
    ingested = int(payload.get("total_ingested") or 0)

    interval_str = f"{float(interval):.0f}s" if interval is not None else "?"
    typer.echo(f"catchem awareness (live) · {total} sources · poll every {interval_str}")
    typer.echo("\nsources by parser:")
    for parser, count in sorted(by_parser.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {parser:<12}  {count:>4}")
    if not by_parser:
        typer.echo("  (none — poller not configured)")
    lag_str = f"{float(median_lag):.0f}s" if median_lag is not None else "n/a (no fresh items this tick)"
    window_str = f"{float(window):.0f}s" if window is not None else "n/a"
    typer.echo("\nfreshness:")
    typer.echo(f"  median publisher lag   {lag_str}")
    typer.echo(f"  effective window       {window_str}")
    typer.echo(f"  total ingested         {ingested:,}")


@app.command("awareness")
def cli_awareness(
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Skip the static catalog and query the running sidecar's "
            "/api/news/awareness endpoint for the live awareness window "
            "(median publisher lag + effective window + total ingested)."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """How broad + fresh is the awareness layer — sources x parsers x cadence.

    Static mode (default) needs no sidecar: it imports the configured feed
    set directly, tallies sources by parser, and prints the poll interval
    from settings. Mirrors /api/news/awareness's breadth side offline.

    With ``--live`` it instead asks the running sidecar what the awareness
    window actually looks like right now (publisher lag + effective window +
    items ingested this process). Same text/JSON shape contract as the HTTP
    twin so scripts can pipe it through ``jq``. Unreachable sidecar → exit 2.
    """
    if live:
        _awareness_live(json_out)
        return

    from .news_poller import assemble_feeds

    settings = load_settings()
    feeds = assemble_feeds()
    sources_by_parser: dict[str, int] = {}
    for spec in feeds:
        key = getattr(spec, "parser", "rss") or "rss"
        sources_by_parser[key] = sources_by_parser.get(key, 0) + 1
    interval = settings.news.poll_interval_seconds

    if json_out:
        typer.echo(json.dumps({
            "schema_version": 1,
            "configured": True,
            "sources_total": len(feeds),
            "sources_by_parser": sources_by_parser,
            "poll_interval_seconds": interval,
        }))
        return

    typer.echo(
        f"catchem awareness · {len(feeds)} sources · poll every {interval:.0f}s\n"
    )
    typer.echo("sources by parser:")
    for parser, count in sorted(sources_by_parser.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {parser:<12}  {count:>4}")
    if not sources_by_parser:
        typer.echo("  (none configured)")


@app.command("signals")
def cli_signals(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
    diagnostics: bool = typer.Option(
        False,
        "--diagnostics",
        "-d",
        help=(
            "Skip the static catalog and instead query the running sidecar's "
            "/api/quant/diagnostics endpoint for the live fail-soft failure "
            "buffer (last 50 signal exceptions + per-signal counts)."
        ),
    ),
) -> None:
    """List every quant signal — name · HTTP endpoint · 1-line summary.

    Useful as a catalog for shell-based exploration. Static catalog (compiled
    in), so no sidecar required — instant lookup of what's available without
    spelunking docs/ARCHITECTURE.md.

    With ``--diagnostics`` (v73), the static catalog is bypassed and the
    command instead asks the running sidecar what's actually failing right
    now. Healthy steady state prints "all signals nominal — buffer empty";
    a degraded run prints the per-signal failure counts plus the most
    recent error class/message so the operator can decide whether to dig
    into the rotating log for the full traceback. Same shape (text + JSON)
    as the HTTP twin so scripts can pipe it through ``jq``.
    """
    if diagnostics:
        _signals_diagnostics(json_out)
        return
    catalog = [
        ("event_clustering", "/api/quant/clusters", "Weighted-Jaccard clusters of related records"),
        ("anomaly", "/api/quant/anomalies", "Z-score volume bursts per (asset_class, reason_code)"),
        ("regime", "/api/quant/regime", "KL-divergence shifts in topic distribution per bucket"),
        ("spillover", "/api/quant/spillover", "Granger-style cross-asset news flow lead/lag edges"),
        ("market_reaction", "/api/quant/reaction/{capture_id}", "Per-record symbol price reaction window"),
        ("source_reliability", "/api/quant/sources", "Per-domain track record + composite reliability"),
        ("novelty", "/api/quant/novelty", "Cosine-distance from rolling baseline (new vs noise)"),
        ("lead_lag", "/api/quant/lead-lag", "Per-domain advance/delay vs cluster centroid"),
        ("co_occurrence", "/api/quant/co-occurrence", "Asset×reason heatmap with χ² lift"),
        ("sentiment_momentum", "/api/quant/sentiment-momentum", "Fast/slow EMA divergence per ticker"),
        ("sentiment_dispersion", "/api/quant/sentiment-dispersion", "Shannon entropy over pos/neutral/neg"),
        ("intensity", "/api/quant/intensity", "Relevance × |sentiment| weighted attention metric"),
        ("market_time", "/api/quant/market-time", "NYSE-session bucketed volume + relevance"),
        ("arrival_heatmap", "/api/quant/arrival-heatmap", "24h × 7day arrival volume heatmap"),
        ("news_velocity", "/api/quant/news-velocity", "Rate + EMA + acceleration regime classifier"),
        ("symbol_correlation", "/api/quant/symbol-correlation", "Pearson r of co-mention vectors per pair"),
        ("persistence", "/api/quant/persistence", "Days-covered ratio — long-running narratives"),
        ("backtest", "/api/backtest", "Stub-vs-DeepSeek calibration vs ground truth"),
    ]
    if json_out:
        typer.echo(json.dumps({
            "schema_version": 1,
            "count": len(catalog),
            "signals": [
                {"name": n, "endpoint": e, "summary": s}
                for (n, e, s) in catalog
            ],
        }))
        return
    typer.echo(f"catchem quant signals · {len(catalog)} entries\n")
    for name, endpoint, summary in catalog:
        typer.echo(f"  {name:<22}  {endpoint:<42}  {summary}")


@app.command("top-recent")
def cli_top_recent(
    limit: int = typer.Option(10, "--limit", "-l", help="Max records to show."),
    min_score: float = typer.Option(0.5, "--min-score", "-m", help="Minimum finance-relevance score."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Show the highest-scoring recent records — analyst attention triage.

    Pipes nicely: ``catchem top-recent --min-score 0.8`` returns only the very
    high-relevance arrivals; default 0.5 surfaces the solid-middle and above.
    Works offline (direct Storage read), no sidecar required.
    """
    storage = _open_storage()
    try:
        records = storage.recent_records(200, relevant_only=False)
    finally:
        storage.close()

    filtered = [
        r for r in records
        if (r.get("finance_relevance_score") or 0.0) >= min_score
    ]
    filtered.sort(key=lambda r: float(r.get("finance_relevance_score") or 0.0), reverse=True)
    top = filtered[:limit]

    if json_out:
        payload = {
            "limit": limit,
            "min_score": min_score,
            "count": len(top),
            "items": [
                {
                    "capture_id": r.get("capture_id"),
                    "title": r.get("title"),
                    "domain": r.get("domain"),
                    "url": r.get("url"),
                    "score": r.get("finance_relevance_score"),
                    "sentiment": r.get("sentiment_label"),
                    "asset_classes": r.get("asset_classes") or [],
                    "symbols": r.get("candidate_symbols") or [],
                    "published_ts": r.get("published_ts"),
                }
                for r in top
            ],
        }
        typer.echo(json.dumps(payload, default=str))
        return

    if not top:
        typer.echo(f"(no records with score ≥ {min_score:.2f} in last 200)")
        return
    for r in top:
        score = r.get("finance_relevance_score") or 0.0
        title = (r.get("title") or "(no title)")[:90]
        domain = r.get("domain") or "?"
        sent = r.get("sentiment_label") or "—"
        syms = ",".join((r.get("candidate_symbols") or [])[:3]) or "—"
        typer.echo(f"  [{score:.2f}] {sent:<8}  {title}  · {domain}  · {syms}")


# Mega-cap fallback when the operator hasn't configured a priority watchlist
# yet — verbatim from api.py's `_DEFAULT_WATCH_TERMS` so the CLI and the HTTP
# twin (/api/news/coverage-gaps) answer against the same out-of-the-box set.
_DEFAULT_WATCH_TERMS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
)


@app.command("coverage-gaps")
def cli_coverage_gaps(
    window_hours: float = typer.Option(
        24.0, "--window-hours", "-w", min=0.1,
        help="Coverage horizon in hours — a watched term with no mention this fresh is a blind spot.",
    ),
    limit: int = typer.Option(
        500, "--limit", "-l", min=1, max=2000,
        help="Records pulled from storage for the scan.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Awareness BLIND-SPOT detector — which watched terms have NO recent coverage?

    Inverts the firehose question. With hundreds of sources arriving, the
    valuable thing to know is "what am I *not* seeing?". Reads recent records
    straight from Storage (offline — no sidecar), takes the watchlist from
    ``settings.news.priority_tickers`` (falling back to a small mega-cap set
    when unconfigured), and classifies every watched term as either *covered*
    (with freshness + mention count) or a *gap*. Mirrors /api/news/coverage-gaps.
    """
    from .awareness_gaps import find_coverage_gaps

    settings = load_settings()
    watch_terms = list(settings.news.priority_tickers) or list(_DEFAULT_WATCH_TERMS)
    window_seconds = float(window_hours) * 3600.0

    storage = _open_storage()
    try:
        records = storage.recent_records(limit, relevant_only=False)
    finally:
        storage.close()

    now = datetime.now(UTC)
    result = find_coverage_gaps(
        records,
        watch_terms,
        window_seconds=window_seconds,
        now=now,
    )

    if json_out:
        payload = {
            "schema_version": 1,
            "window_hours": window_hours,
            "limit": limit,
            "watch_terms": watch_terms,
            **result,
        }
        typer.echo(json.dumps(payload, default=str))
        return

    covered = result["covered"]
    gaps = result["gaps"]
    typer.echo(
        f"catchem coverage-gaps · {len(watch_terms)} watched · "
        f"window {window_hours:g}h · scanned {len(records)} record(s)\n"
    )

    typer.echo(f"gaps ({len(gaps)}) — no coverage in window:")
    if gaps:
        for term in gaps:
            typer.echo(f"  ✗ {term}")
    else:
        typer.echo("  (none — every watched term has fresh coverage)")

    typer.echo(f"\ncovered ({len(covered)}) — freshest first:")
    if covered:
        for c in covered:
            age_s = float(c["last_seen_age_seconds"] or 0.0)
            if age_s < 3600:
                age_str = f"{age_s / 60:.0f}m ago"
            elif age_s < 86400:
                age_str = f"{age_s / 3600:.1f}h ago"
            else:
                age_str = f"{age_s / 86400:.1f}d ago"
            typer.echo(f"  ✓ {c['term']:<10}  {age_str:<10}  ({c['mention_count']} mention(s))")
    else:
        typer.echo("  (none — no watched term seen in window)")


@app.command("status")
def cli_status(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """One-line health summary: records, tags, schema version, last poll, sidecar pid.

    Reads SQLite directly + probes the local sidecar (best-effort) — does NOT
    spawn one. Useful for shell pipelines and quick checks: ``catchem status``
    prints a single human-readable line; ``catchem status --json`` emits a
    machine-parseable envelope for monitoring scripts.

    The sidecar probe is a 0.5s GET against /healthz on 127.0.0.1:8087; absent
    or slow → reports ``sidecar: stopped`` rather than failing.
    """
    from .migrations import current_version

    storage = _open_storage()
    try:
        with storage._lock, storage._connection() as conn:
            records = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            try:
                tags = conn.execute("SELECT COUNT(*) FROM record_tags").fetchone()[0]
            except Exception:
                tags = 0
            schema_version = current_version(conn)
            try:
                last_row = conn.execute(
                    "SELECT created_at FROM records ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                last_record = last_row[0] if last_row else None
            except Exception:
                last_record = None
    finally:
        storage.close()

    # Best-effort sidecar probe — short timeout, never raises.
    sidecar_pid: int | None = None
    sidecar_ok = False
    try:
        import httpx
        settings = load_settings()
        # v66 audit fix: the sub-model lives at `settings.api.host/port`, NOT
        # top-level. Pre-fix this fell through to defaults silently and reported
        # `sidecar: stopped` for non-default binds.
        host = settings.api.host or "127.0.0.1"
        port = settings.api.port or 8087
        resp = httpx.get(f"http://{host}:{port}/healthz", timeout=0.5)
        sidecar_ok = resp.status_code == 200
        if sidecar_ok:
            try:
                stats_resp = httpx.get(f"http://{host}:{port}/api/stats", timeout=0.5)
                if stats_resp.status_code == 200:
                    body = stats_resp.json()
                    sidecar_pid = body.get("process", {}).get("pid") or body.get("pid")
            except Exception:
                sidecar_pid = None
    except Exception:
        sidecar_ok = False

    payload = {
        "records": int(records or 0),
        "tags": int(tags or 0),
        "schema_version": int(schema_version),
        "last_record_at": last_record,
        "sidecar_ok": sidecar_ok,
        "sidecar_pid": sidecar_pid,
        "generated_at": datetime.now(UTC).isoformat(),
    }

    if json_out:
        typer.echo(json.dumps(payload, default=str))
        return

    sidecar_str = (
        f"sidecar: ok pid {sidecar_pid}" if sidecar_ok and sidecar_pid
        else "sidecar: ok" if sidecar_ok
        else "sidecar: stopped"
    )
    last_str = f" · last {last_record[:19]}" if last_record else ""
    typer.echo(
        f"[catchem v{schema_version}] {records:,} records · {tags:,} tags · {sidecar_str}{last_str}"
    )


@app.command("ping-deepseek")
def cli_ping_deepseek(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Test DeepSeek API credentials with a minimal /chat/completions call.

    Reads the API key from `CATCHEM_REVIEWERS__DEEPSEEK__API_KEY` (or the
    `reviewers.deepseek.api_key` settings value). Sends a 1-token completion
    so verification stays sub-cent. Exit 0 if the key is accepted, 1 otherwise.
    """
    import httpx

    settings = load_settings()
    cfg = settings.reviewers.deepseek
    if not cfg.api_key:
        typer.echo("error: no DeepSeek api key configured (set CATCHEM_REVIEWERS__DEEPSEEK__API_KEY)", err=True)
        raise typer.Exit(1)

    body = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        typer.echo(f"error: transport failure: {exc}", err=True)
        raise typer.Exit(1) from exc

    ok = resp.status_code == 200
    result = {
        "ok": ok,
        "status": resp.status_code,
        "model": cfg.model,
        "base_url": cfg.base_url,
    }
    if not ok:
        # Surface a short error excerpt so 401 vs 429 vs 5xx is visible.
        result["error"] = resp.text[:200]

    if json_out:
        typer.echo(json.dumps(result, indent=2))
    else:
        if ok:
            typer.echo(f"ok: DeepSeek accepted credentials (model={cfg.model}, http {resp.status_code})")
        else:
            typer.echo(f"fail: DeepSeek http {resp.status_code} — {result.get('error') or '(no body)'}", err=True)

    if not ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
