"""Structured logging setup. JSON to file, human-readable to stderr."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

_CONFIGURED = False

# Sidecar log rotation policy. 5 MB × (1 live + 3 backups) ≈ 20 MB worst-case
# on disk — small enough to fit happily under macOS `~/Library/Logs/Catchem/`
# but large enough to retain an hour-plus of structured logs at the chatty
# DEBUG levels we occasionally enable for postmortems. ``RotatingFileHandler``
# rolls the live file to ``catchem.log.1`` once it crosses ``maxBytes``;
# subsequent rolls shift older backups down (``.1`` -> ``.2`` -> ``.3``) and
# delete the oldest so we never grow unbounded.
LOG_ROTATION_MAX_BYTES = 5 * 1024 * 1024
LOG_ROTATION_BACKUP_COUNT = 3


def configure_logging(level: str = "INFO", log_file: Path | None = None, json_mode: bool = True) -> None:
    """Idempotent logging configuration. Safe to call from CLI, API, and tests.

    When ``log_file`` is provided the file is opened with a
    :class:`logging.handlers.RotatingFileHandler` (5 MB cap, 3 backups). The
    handler is a drop-in for stdlib ``FileHandler`` so the JSON-line format
    written by structlog's :class:`~structlog.processors.JSONRenderer` (or
    the ConsoleRenderer when ``json_mode=False``) survives a rollover —
    each rotated file remains a valid sequence of JSON lines.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=LOG_ROTATION_MAX_BYTES,
                backupCount=LOG_ROTATION_BACKUP_COUNT,
                encoding="utf-8",
            )
        )

    logging.basicConfig(level=log_level, handlers=handlers, format="%(message)s", force=True)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_mode:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
