"""Runtime configuration. Loads configs/fusion.yaml (lowest priority), then env vars,
then `.env`. The CLI may pass an explicit config path.

Priority (lowest → highest):
  configs/fusion.yaml  <  process env  <  .env file  <  CLI overrides
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FusionMode(str, Enum):
    PRODUCTION_SAFE = "production_safe"
    REPLAY_EXISTING = "replay_existing"
    LIVE_TAIL = "live_tail"
    RESEARCH_DIAGNOSTIC = "research_diagnostic"


def project_root() -> Path:
    """fusion_stack project root (parent of src/)."""
    return Path(__file__).resolve().parents[2]


def _yaml_overrides(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = project_root() / "configs" / "fusion.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"fusion config at {path} did not parse to a mapping")
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
    allow_research_diagnostic_in_modes: list[str] = Field(
        default_factory=lambda: ["research_diagnostic"]
    )
    abort_if_governance_state_changed: bool = True


class ReplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_size: int = 32
    awareness_jsonl_glob: str = "data/jsonl/captures/**/*.jsonl"
    text_excerpt_chars: int = 2000
    offset_persist_seconds: float = 5.0


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_seconds: float = 10.0
    tail_max_per_tick: int = 50
    awareness_api_url: str = "http://127.0.0.1:8085"


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = 8087
    cors_origins: list[str] = Field(default_factory=list)
    recent_limit_default: int = 50
    recent_limit_max: int = 500


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sqlite_url: str = "sqlite:///data/db/fusion.sqlite3"
    parquet_results_dir: str = "data/results"
    dlq_dir: str = "data/results/dlq"
    vector_index_dir: str = "data/vector_index"
    rotate_parquet_records: int = 5000


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    level: str = "INFO"
    json_logs: bool = Field(default=True, alias="json")
    file: str = "data/logs/fusion_stack.log"


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
    # 30s default: 18 feeds × ~5 new items/min cumulative makes the visible
    # rate ~2 new items per tick — frequent enough to look alive without
    # hammering publishers (each is still polled at most twice per minute).
    poll_interval_seconds: float = 30.0
    # Built-in defaults defined in news_poller.DEFAULT_FEEDS. Use this to
    # supply or replace the source set: list of {name, url, fallback_domain}.
    feeds: list[dict[str, str]] = Field(default_factory=list)


class PathConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    awareness_repo: Path = Field(default_factory=lambda: Path("/tmp/awareness-missing"))
    newsimpact_repo: Path = Field(default_factory=lambda: Path("/tmp/merged_news-missing"))
    awareness_data_dir: Path = Field(default_factory=lambda: Path("/tmp/awareness-missing/data"))
    fusion_output_dir: Path = Field(default_factory=lambda: project_root() / "data")

    @field_validator("awareness_repo", "newsimpact_repo", "awareness_data_dir", "fusion_output_dir", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> Path:
        return Path(v).expanduser() if v is not None else v


class Settings(BaseSettings):
    """Top-level settings. Aggregates sub-configs and applies env overrides.

    Precedence (lowest → highest):
      defaults  <  configs/fusion.yaml (via init kwargs)  <  env vars  <  .env file
    """

    model_config = SettingsConfigDict(
        env_prefix="FUSION_",
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
        #   defaults  <  configs/fusion.yaml (init kwargs)  <  .env file  <  process env
        #
        # Process env must beat .env so:
        #   (a) explicit shell overrides win (operator intent),
        #   (b) pytest's monkeypatch.setenv works as expected (test ergonomics),
        #   (c) CI's job env beats any committed .env (deployment determinism).
        return env_settings, dotenv_settings, init_settings, file_secret_settings

    mode: FusionMode = FusionMode.PRODUCTION_SAFE
    paths: PathConfig = Field(default_factory=PathConfig)
    models_: ModelConfig = Field(default_factory=ModelConfig, alias="models")
    guards: GuardConfig = Field(default_factory=GuardConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging_: LoggingConfig = Field(default_factory=LoggingConfig, alias="logging")
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    kaggle: KaggleConfig = Field(default_factory=KaggleConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)

    use_ml_stubs: bool | None = None  # convenience flat env override

    @model_validator(mode="after")
    def _propagate_flat_overrides(self) -> "Settings":
        """Wire the documented flat env vars into the nested sub-configs.

        `FUSION_USE_ML_STUBS=false` MUST flip `models_.use_ml_stubs` to False,
        even though the field formally lives on the nested `ModelConfig`. This
        keeps `.env.example` and the docs honest.
        """
        if self.use_ml_stubs is not None:
            self.models_.use_ml_stubs = self.use_ml_stubs
        return self

    # ── derived paths ────────────────────────────────────────────────────────
    def output_path(self, *parts: str) -> Path:
        return self.paths.fusion_output_dir.joinpath(*parts)

    def sqlite_path(self) -> Path:
        url = self.storage.sqlite_url
        if url.startswith("sqlite:///"):
            rel = url.removeprefix("sqlite:///")
            p = Path(rel)
            if not p.is_absolute():
                p = self.paths.fusion_output_dir / Path(rel).relative_to("data") if rel.startswith("data/") else self.paths.fusion_output_dir.parent / p
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        raise ValueError(f"unsupported sqlite_url: {url}")

    def is_production_safe(self) -> bool:
        return self.mode == FusionMode.PRODUCTION_SAFE

    def diagnostic_allowed(self) -> bool:
        """True only when mode is research_diagnostic AND the guard flag is set AND
        the configured diagnostic-allowed list includes the mode."""
        if not self.guards.newsimpact_diagnostic_enabled:
            return False
        if self.mode == FusionMode.PRODUCTION_SAFE:
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
    "FusionMode",
    "Settings",
    "load_settings",
    "reload_settings",
    "project_root",
]
