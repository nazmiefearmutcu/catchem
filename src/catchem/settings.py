"""Runtime configuration. Loads configs/catchem.yaml (lowest priority), then env vars,
then `.env`. The CLI may pass an explicit config path.

Priority (lowest → highest):
  configs/catchem.yaml  <  process env  <  .env file  <  CLI overrides
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Same UP042 deferral as schemas.py — (str, Enum) preserves the qualified
# repr behaviour ("CatchemMode.PRODUCTION_SAFE") that any external consumer
# of stringified mode values might depend on. .value access is uniform
# across (str, Enum) and StrEnum.
class CatchemMode(str, Enum):  # noqa: UP042
    PRODUCTION_SAFE = "production_safe"
    REPLAY_EXISTING = "replay_existing"
    LIVE_TAIL = "live_tail"
    RESEARCH_DIAGNOSTIC = "research_diagnostic"


def project_root() -> Path:
    """catchem project root (parent of src/)."""
    return Path(__file__).resolve().parents[2]


def _yaml_overrides(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = project_root() / "configs" / "catchem.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"catchem config at {path} did not parse to a mapping")
    
    # Defensive schema validation: check that all keys in the YAML file are valid fields of Settings
    # This prevents misspelled or corrupt top-level keys from being silently ignored.
    invalid_keys = [k for k in data if k not in Settings.model_fields]
    if invalid_keys:
        raise ValueError(f"Invalid configuration keys in {path}: {', '.join(invalid_keys)}")
        
    return data


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentiment_default: str = "ProsusAI/finbert"
    sentiment_alternates: list[str] = Field(default_factory=list)
    zero_shot: str = "facebook/bart-large-mnli"
    embedding: str = "sentence-transformers/all-MiniLM-L6-v2"
    reranker: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    use_ml_stubs: bool = True


class GuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    newsimpact_diagnostic_enabled: bool = False
    allow_research_diagnostic_in_modes: list[str] = Field(default_factory=lambda: ["research_diagnostic"])
    abort_if_governance_state_changed: bool = True


class ReplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_size: int = 32
    # Glob that scopes which finalized JSONL files a directory replay ingests.
    # Resolved RELATIVE to the discovered awareness JSONL root (see
    # Supervisor.run_replay → ReplayConfig.replay_pattern). The historical
    # default carried a `data/jsonl/captures/` prefix that duplicated the root
    # discovery; only the trailing portion past a `jsonl/` segment is used so
    # the knob now actually narrows the scan instead of being a silent no-op.
    awareness_jsonl_glob: str = "data/jsonl/captures/**/*.jsonl"
    text_excerpt_chars: int = 2000
    offset_persist_seconds: float = 5.0

    def replay_pattern(self) -> str:
        """The glob to hand ReplayRunner, relative to the discovered root.

        ``discover_awareness_jsonl_root`` already resolves down to the
        ``…/jsonl`` directory, so a configured glob like
        ``data/jsonl/captures/**/*.jsonl`` is trimmed to the part AFTER the
        first ``jsonl/`` segment (``captures/**/*.jsonl``) to avoid a
        double-``jsonl`` path that would match nothing. Globs without a
        ``jsonl/`` segment are used verbatim. Empty / blank falls back to the
        recursive default so a mis-set value never silently matches zero files.
        """
        raw = (self.awareness_jsonl_glob or "").strip()
        if not raw:
            return "**/*.jsonl"
        marker = "jsonl/"
        idx = raw.find(marker)
        if idx != -1:
            tail = raw[idx + len(marker) :]
            return tail or "**/*.jsonl"
        return raw


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_seconds: float = 10.0
    tail_max_per_tick: int = 50
    awareness_api_url: str = "http://127.0.0.1:8085"
    ingestion_queue_enabled: bool = False


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = 8087
    cors_origins: list[str] = Field(
        default_factory=lambda: ["tauri://localhost", "http://127.0.0.1:8087", "http://localhost:8087"]
    )
    recent_limit_default: int = 50
    recent_limit_max: int = 500
    # Maximum payload size in bytes for database file imports (e.g. SQLite backups)
    max_import_size_bytes: int = 200 * 1024 * 1024
    # Maximum payload size in bytes for demo uploads/pastes
    max_upload_size_bytes: int = 5 * 1024 * 1024


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sqlite_url: str = "sqlite:///data/db/catchem.sqlite3"
    parquet_results_dir: str = "data/results"
    dlq_dir: str = "data/results/dlq"
    vector_index_dir: str = "data/vector_index"
    rotate_parquet_records: int = 5000
    wal_autocheckpoint: int = 10000


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    level: str = "INFO"
    json_logs: bool = Field(default=True, alias="json")
    file: str = "data/logs/catchem.log"


class ThresholdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finance_relevance_floor: float = 0.35
    asset_class_min: float = 0.25
    reason_code_min: float = 0.25
    evidence_top_k: int = 3


class KaggleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enable_downloads: bool = False
    datasets: list[str] = Field(default_factory=list)


class NewsConfig(BaseModel):
    """Background RSS poller — keeps the Live Feed live in the Catchem .app.

    Set `poller_enabled=false` to skip ingestion entirely (CI/tests do this).
    """

    model_config = ConfigDict(extra="forbid")
    poller_enabled: bool = True
    # 10s default with 50+ sources active. Each individual publisher is
    # polled at most 6x/min — still polite. The 10s figure is the floor
    # below which the asyncio loop starts overlapping fetch cycles.
    poll_interval_seconds: float = 10.0
    # Built-in defaults defined in news_poller.DEFAULT_FEEDS. Use this to
    # supply or replace the source set: list of {name, url, fallback_domain}.
    feeds: list[dict[str, str]] = Field(default_factory=list)
    # Tickers/companies to actively watch via dynamic per-entity Google News
    # queries (the watchlist_dynamic source pack reads this). Each becomes a
    # near-real-time GN search feed so the system tracks exactly what the
    # operator cares about, not just the curated mainstream surface. Empty =
    # the pack falls back to a built-in mega-cap default set.
    priority_tickers: list[str] = Field(default_factory=list)
    # Cross-source near-duplicate suppression: when many outlets carry the
    # same story, collapse items whose normalized titles match within this
    # window so the feed shows the story once instead of N times. 0 disables.
    dedup_title_window_seconds: float = 21600.0  # 6h
    # Adaptive per-source polling: persistently-empty feeds (HTTP-200 but zero
    # new items, cycle after cycle) back off to a longer cadence while
    # high-yield feeds keep polling every cycle. Separate from the error
    # circuit breaker (failures). False = poll every feed every cycle.
    adaptive_polling_enabled: bool = True
    # Maximum response body size in bytes to prevent OOM on excessively large feeds.
    max_response_size_bytes: int = 10 * 1024 * 1024
    # Real-time WebSocket PUSH channel (ws_push.WebSocketNewsChannel) —
    # complements the HTTP poller for genuine push firehoses (squawk/news WS)
    # so the freshest sources arrive with near-zero latency. OFF by default;
    # the operator opts in AND supplies sources. Empty sources = the channel
    # constructs but connects to nothing. No hardcoded endpoints ship.
    websocket_enabled: bool = False
    # List of {name, url, fallback_domain} dicts. Each becomes a long-lived
    # WebSocket reader task. Default empty so nothing connects out of the box.
    websocket_sources: list[dict[str, str]] = Field(default_factory=list)


class ArchiveConfig(BaseModel):
    """Drive archiver — drains old SQLite rows into a CSV on cloud-sync storage.

    Keeps the local working set tight (`local_cap_rows`) while preserving
    the full ingestion history in a flat CSV that opens in Excel/Sheets.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    # Path to the cloud-sync folder. If empty, archive.detect_drive_dir()
    # auto-detects Google Drive Desktop → iCloud Drive → ~/Documents/Catchem.
    drive_dir: str = ""
    # How many recent rows to keep in SQLite. Everything older is drained
    # to CSV. 150 is a comfortable cap that always covers a few minutes of
    # ingest on the busy default feed set while keeping the .sqlite3 file
    # tiny (≈300 KB).
    local_cap_rows: int = 150
    # How often the archiver wakes up. 30s is a sweet spot — the news
    # poller adds ~50 rows per minute, so 30s keeps the local count
    # oscillating between ~150 and ~175.
    interval_seconds: float = 30.0


class DeepSeekReviewerConfig(BaseModel):
    """DeepSeek hosted-LLM second-opinion reviewer.

    Wiring:
      * `enabled` is the operator toggle — false reverts catchem to
        fully offline behavior (no external HTTP).
      * `api_key` is read from `CATCHEM_REVIEWERS__DEEPSEEK__API_KEY`
        env var (set the .env or your shell). Empty string disables the
        reviewer at runtime even if `enabled=true`.
      * `sampling_rate` controls how many ingested captures get a
        second opinion. 0.10 means 1 in 10 articles. The selection is
        DETERMINISTIC (SHA-256 over capture_id) so replays don't
        re-spend budget on different rows.
      * `usd_cap` is the safety net. Once the cumulative DeepSeek spend
        reaches this number, new calls fail fast with `budget_exceeded`.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    enabled: bool = False
    api_key: str = ""
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    sampling_rate: float = 0.10
    usd_cap: float = 9.50
    max_output_tokens: int = 600
    timeout_seconds: float = 30.0


class ReviewersConfig(BaseModel):
    """Aggregates second-opinion reviewers. Primary pipeline lives outside this block."""

    model_config = ConfigDict(extra="forbid")
    deepseek: DeepSeekReviewerConfig = Field(default_factory=DeepSeekReviewerConfig)


class WebhookConfig(BaseModel):
    """Slack/Discord/Teams-compatible incoming webhook output.

    When `enabled=true`, every record finalized through the supervisor
    ingestion path that clears the configured score floor (and optional
    asset-class / reason-code filters) is POSTed to `url` as a
    Slack-shape `{text, blocks}` JSON payload.

    Posts run in a background thread — webhook latency or 4xx/5xx
    responses must NEVER block ingestion. Failures are logged and
    swallowed; the supervisor moves on. The URL is held in sidecar
    memory only (it's a soft secret — Slack URLs encode an auth token
    in the path) and is intentionally absent from `exportSnapshot()`.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    url: str = ""  # Slack/Discord/Teams incoming webhook URL
    min_score: float = 0.7  # Only POST records with finance_relevance_score >= this
    asset_class_filter: list[str] | None = None  # None = any
    reason_code_filter: list[str] | None = None
    timeout_seconds: float = 5.0


class PathConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    awareness_repo: Path = Field(default_factory=lambda: Path("/tmp/awareness-missing"))
    newsimpact_repo: Path = Field(default_factory=lambda: Path("/tmp/merged_news-missing"))
    awareness_data_dir: Path = Field(default_factory=lambda: Path("/tmp/awareness-missing/data"))
    catchem_output_dir: Path = Field(default_factory=lambda: project_root() / "data")

    @field_validator(
        "awareness_repo", "newsimpact_repo", "awareness_data_dir", "catchem_output_dir", mode="before"
    )
    @classmethod
    def _coerce_path(cls, v: Any) -> Path:
        return Path(v).expanduser() if v is not None else v


class Settings(BaseSettings):
    """Top-level settings. Aggregates sub-configs and applies env overrides.

    Precedence (lowest → highest):
      defaults  <  configs/catchem.yaml (via init kwargs)  <  env vars  <  .env file
    """

    model_config = SettingsConfigDict(
        env_prefix="CATCHEM_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Precedence (lowest → highest):
        #   defaults  <  configs/catchem.yaml (init kwargs)  <  .env file  <  process env
        #
        # Process env must beat .env so:
        #   (a) explicit shell overrides win (operator intent),
        #   (b) pytest's monkeypatch.setenv works as expected (test ergonomics),
        #   (c) CI's job env beats any committed .env (deployment determinism).
        return env_settings, dotenv_settings, init_settings, file_secret_settings

    mode: CatchemMode = CatchemMode.PRODUCTION_SAFE
    paths: PathConfig = Field(default_factory=PathConfig)
    # Field name is `models` (not `models_`) so pydantic-settings derives the
    # nested env var path correctly: `CATCHEM_MODELS__USE_ML_STUBS` →
    # `Settings.models.use_ml_stubs`. With the previous alias-only setup the
    # env var was silently ignored — nested env lookup uses the field NAME,
    # not the alias.
    models: ModelConfig = Field(default_factory=ModelConfig)
    guards: GuardConfig = Field(default_factory=GuardConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    # Same fix as `models` above. Safe to name the field `logging` here —
    # settings.py does not import the stdlib `logging` module at top level.
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    kaggle: KaggleConfig = Field(default_factory=KaggleConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    # Second-opinion reviewers (DeepSeek etc). Default is fully disabled
    # so production-safe behavior is preserved unless the operator opts in.
    reviewers: ReviewersConfig = Field(default_factory=ReviewersConfig)
    # Slack/Discord/Teams webhook output for high-relevance arrivals.
    # OFF by default; the operator opts in via Settings → Webhook output.
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)

    use_ml_stubs: bool | None = None  # convenience flat env override

    @model_validator(mode="after")
    def _propagate_flat_overrides(self) -> Settings:
        """Wire the documented flat env vars into the nested sub-configs.

        `CATCHEM_USE_ML_STUBS=false` MUST flip `models.use_ml_stubs` to False,
        even though the field formally lives on the nested `ModelConfig`. This
        keeps `.env.example` and the docs honest.
        """
        if self.use_ml_stubs is not None:
            self.models.use_ml_stubs = self.use_ml_stubs
        return self

    # ── derived paths ────────────────────────────────────────────────────────
    def output_path(self, *parts: str) -> Path:
        return self.paths.catchem_output_dir.joinpath(*parts)

    def sqlite_path(self) -> Path:
        url = self.storage.sqlite_url
        if url.startswith("sqlite:///"):
            rel = url.removeprefix("sqlite:///")
            p = Path(rel)
            if not p.is_absolute():
                p = (
                    self.paths.catchem_output_dir / Path(rel).relative_to("data")
                    if rel.startswith("data/")
                    else self.paths.catchem_output_dir.parent / p
                )
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        raise ValueError(f"unsupported sqlite_url: {url}")

    def is_production_safe(self) -> bool:
        return self.mode == CatchemMode.PRODUCTION_SAFE

    def diagnostic_allowed(self) -> bool:
        """True only when mode is research_diagnostic AND the guard flag is set AND
        the configured diagnostic-allowed list includes the mode."""
        if not self.guards.newsimpact_diagnostic_enabled:
            return False
        if self.mode == CatchemMode.PRODUCTION_SAFE:
            return False
        return self.mode.value in self.guards.allow_research_diagnostic_in_modes


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=4)
def load_settings(config_path: Path | None = None) -> Settings:
    """Build settings: YAML defaults → env → .env. Cached by config path."""
    yaml_data = _yaml_overrides(config_path)
    # Pydantic-settings handles env+.env automatically; we just seed yaml values
    # by passing them as constructor kwargs (env will override).
    return Settings(**yaml_data)


def reload_settings() -> None:
    """Drop the cache. Used by tests."""
    load_settings.cache_clear()


__all__ = [
    "CatchemMode",
    "Settings",
    "load_settings",
    "project_root",
    "reload_settings",
]
