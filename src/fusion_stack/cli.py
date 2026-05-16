"""Typer-based CLI. Wraps Supervisor and a few maintenance scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from .bootstrap import bootstrap as bootstrap_call
from .logging import configure_logging, get_logger
from .settings import FusionMode, load_settings, reload_settings
from .supervisor import Supervisor

app = typer.Typer(no_args_is_help=True, help="fusion_stack: finance-relevance layer over Awareness.")
logger = get_logger("fusion.cli")


def _override_mode(mode: Optional[str]) -> None:
    if mode is not None:
        import os
        os.environ["FUSION_MODE"] = mode
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
        if s.mode == FusionMode.LIVE_TAIL:
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
        os.environ["FUSION_PATHS__AWARENESS_DATA_DIR"] = str(path.parent)
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
    """Initialize fusion_stack directories and verify guard. Idempotent."""
    summary = bootstrap_call(skip_warm=skip_warm)
    typer.echo(json.dumps(summary, indent=2))


if __name__ == "__main__":
    app()
