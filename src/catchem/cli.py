"""Typer-based CLI. Wraps Supervisor and a few maintenance scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from .bootstrap import bootstrap as bootstrap_call
from .logging import configure_logging, get_logger
from .settings import CatchemMode, load_settings, reload_settings
from .supervisor import Supervisor

app = typer.Typer(no_args_is_help=True, help="catchem: finance-relevance layer over Awareness.")
logger = get_logger("catchem.cli")


def _override_mode(mode: Optional[str]) -> None:
    if mode is not None:
        import os
        os.environ["CATCHEM_MODE"] = mode
        reload_settings()


@app.command()
def run(
    mode: str = typer.Option("replay_existing", help="production_safe | replay_existing | live_tail | research_diagnostic"),
    max_records: Optional[int] = typer.Option(None, help="Replay limit (replay mode only)."),
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
    path: Optional[Path] = typer.Option(None, help="Path to a JSONL file to replay (overrides discovery)."),
    max_records: Optional[int] = typer.Option(None),
) -> None:
    """Replay a single JSONL file or the default Awareness root."""
    s = load_settings()
    if path:
        # Point the data dir at the file's parent for one-shot use
        import os
        os.environ["CATCHEM_PATHS__AWARENESS_DATA_DIR"] = str(path.parent)
        reload_settings()
        s = load_settings()
    sup = Supervisor(s)
    try:
        counts = sup.run_replay(max_records=max_records)
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
    extended: Optional[Path] = typer.Option(None, help="Optional path to a JSONL of extra golden items."),
) -> None:
    """Throughput sanity check OR golden-set precision/recall/F1."""
    import time

    s = load_settings()
    configure_logging(level=s.logging_.level, json_mode=False)
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


@app.command()
def status() -> None:
    """Print supervisor status as JSON."""
    s = load_settings()
    sup = Supervisor(s)
    try:
        typer.echo(json.dumps(sup.status(), indent=2))
    finally:
        sup.close()


@app.command()
def serve(
    host: Optional[str] = typer.Option(None),
    port: Optional[int] = typer.Option(None),
) -> None:
    """Start the FastAPI HTTP server."""
    import uvicorn

    s = load_settings()
    from .api import create_app

    app_ = create_app(s)
    uvicorn.run(
        app_,
        host=host or s.api.host,
        port=int(port or s.api.port),
        log_level=s.logging_.level.lower(),
    )


@app.command("bootstrap-init")
def bootstrap_init(skip_warm: bool = typer.Option(True, help="Skip HF model warm-cache.")) -> None:
    """Initialize catchem directories and verify guard. Idempotent."""
    summary = bootstrap_call(skip_warm=skip_warm)
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def demo(
    title: str = typer.Option(..., "--title", "-t", help="Headline of the article to ingest."),
    text: Optional[str] = typer.Option(None, "--text", help="Article body. If omitted, read from --text-file or stdin."),
    text_file: Optional[Path] = typer.Option(None, "--text-file", help="Read article body from this file."),
    domain: str = typer.Option("demo.local", "--domain", help="Source domain (sets the domain prior)."),
    url: Optional[str] = typer.Option(None, "--url", help="Canonical URL (sanity-checked by the safeHref filter in UI)."),
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


if __name__ == "__main__":
    app()
