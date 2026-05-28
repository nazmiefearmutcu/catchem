"""Shared pytest fixtures.

Highlights:
  * ``tmp_settings``: a fresh Settings instance pointed at a temp output dir.
  * ``synth_capture``: factory for AwarenessCaptureView (no Awareness install needed).
  * ``real_jsonl_root``: path to actual Awareness captures if they exist locally.
  * ``isolated_env``: scrubs env vars between tests to prevent settings leakage.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from catchem.schemas import AwarenessCaptureView
from catchem.settings import Settings, load_settings, reload_settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEWSIMPACT_DEFAULT = Path("/Users/nazmi/Desktop/Projeler/proje/merged_news")
AWARENESS_DEFAULT = Path("/Users/nazmi/Desktop/Projeler/proje/awareness")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force a clean environment per test. Tests must opt in to specific knobs.

    Tests that change env vars themselves should call ``reload_settings()`` so
    the cached Settings instance is rebuilt. The autouse fixture handles the
    initial setup but not subsequent monkeypatches inside the test body.

    CI escape hatch: when the caller has pre-set ``CATCHEM_PATHS__NEWSIMPACT_REPO``
    or ``CATCHEM_PATHS__AWARENESS_REPO`` in the process env (typical for GitHub
    Actions, which synthesizes a quarantined governance fixture under /tmp),
    we honor that path instead of pointing at the developer's local repos.
    Captured BEFORE the env wipe so the override survives the cleanup.
    """
    ci_newsimpact = os.environ.get("CATCHEM_PATHS__NEWSIMPACT_REPO")
    ci_awareness = os.environ.get("CATCHEM_PATHS__AWARENESS_REPO")

    for k in list(os.environ.keys()):
        if k.startswith("CATCHEM_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_REPO", ci_awareness or str(AWARENESS_DEFAULT))
    monkeypatch.setenv("CATCHEM_PATHS__NEWSIMPACT_REPO", ci_newsimpact or str(NEWSIMPACT_DEFAULT))
    # Point at an empty awareness dir by default so replay tests don't accidentally
    # sweep the entire real repo. Tests that exercise the real repo override this.
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path / "aw"))
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "true")
    monkeypatch.setenv("CATCHEM_LOGGING__LEVEL", "WARNING")
    reload_settings()


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """A Settings instance whose output dir is under tmp_path."""
    reload_settings()
    s = load_settings()
    return s


@pytest.fixture
def synth_capture() -> Callable[..., AwarenessCaptureView]:
    """Factory for synthetic AwarenessCaptureView objects."""

    def _make(
        capture_id: str = "cap-001",
        doc_id: str = "doc-001",
        title: str = "Fed raises rates by 25 bps amid sticky inflation",
        text: str | None = None,
        domain: str = "reuters.com",
        source_type: str = "rss",
        language: str = "en",
        published_ts: datetime | None = None,
    ) -> AwarenessCaptureView:
        body = text or (
            "The Federal Reserve raised its benchmark interest rate by 25 basis "
            "points on Wednesday, citing persistent inflation and a tight labor "
            "market. Equities sold off and Treasury yields jumped on the news. "
            "Chair Powell said the central bank remains data-dependent."
        )
        return AwarenessCaptureView(
            capture_id=capture_id,
            doc_id=doc_id,
            title=title,
            text=body,
            domain=domain,
            url=f"https://{domain}/article/{doc_id}",
            canonical_url=f"https://{domain}/article/{doc_id}",
            source_type=source_type,
            discovery_channel=f"rss:{domain}",
            language=language,
            fetch_ts=datetime.now(UTC),
            observed_ts=datetime.now(UTC),
            published_ts=published_ts,
            content_hash="abc123",
            robots_decision="not_applicable",
        )

    return _make


@pytest.fixture
def synth_non_finance_capture() -> AwarenessCaptureView:
    return AwarenessCaptureView(
        capture_id="cap-sport-001",
        doc_id="doc-sport-001",
        title="Local football team wins championship after dramatic match",
        text=(
            "The scoreboard told the story: a last-minute goal sealed the championship. "
            "Players celebrated with the trophy as fans rushed the field. The coach "
            "praised his squad's effort throughout the season."
        ),
        domain="espn.com",
        url="https://espn.com/article/match-final",
        source_type="rss",
        discovery_channel="rss:espn.com",
        language="en",
        fetch_ts=datetime.now(UTC),
        observed_ts=datetime.now(UTC),
    )


@pytest.fixture
def real_jsonl_root() -> Path | None:
    """Return the awareness JSONL captures root if it exists on this machine."""
    root = AWARENESS_DEFAULT / "data" / "jsonl"
    if root.exists() and any(root.glob("**/*.jsonl")):
        return root
    return None


@pytest.fixture
def write_jsonl(tmp_path: Path) -> Callable[..., Path]:
    """Create a JSONL file from a list of capture dicts and return its path."""

    def _make(rows: list[dict[str, Any]], name: str = "captures.jsonl") -> Path:
        target = tmp_path / "jsonl" / "captures" / "2026" / "05" / "16"
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return path

    return _make
