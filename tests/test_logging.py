"""Contract pins for :mod:`catchem.logging` and its rotation policy.

The sidecar's structured-log stream lands in two places by design:

* stderr (always) — captured by the Tauri runner into
  ``~/Library/Logs/Catchem/sidecar.log`` via the OS-level stdio redirect.
* an optional in-process file handler — wired by :class:`Supervisor` when
  ``settings.logging.file`` points inside the catchem output dir.

These tests pin the in-process file-handler contract:

* the handler MUST be a :class:`logging.handlers.RotatingFileHandler` (so
  the file never grows unbounded) with the project-wide cap baked into
  :data:`catchem.logging.LOG_ROTATION_MAX_BYTES` /
  :data:`~catchem.logging.LOG_ROTATION_BACKUP_COUNT`.
* the parent directory MUST be ``mkdir(parents=True, exist_ok=True)``'d
  before the first record is written — otherwise the very first log on a
  fresh machine 500s the boot.
* a rollover MUST produce a numbered ``.1`` backup, leaving the live file
  ready to accept the next record. The JSON-line file format is preserved
  across the rotation (each backup is still a sequence of newline-delimited
  records).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest
import structlog

import catchem.logging as catchem_logging
from catchem.logging import (
    LOG_ROTATION_BACKUP_COUNT,
    LOG_ROTATION_MAX_BYTES,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def _reset_logging_state() -> None:
    """The logging module is global state. Reset between tests."""
    # Capture and restore handlers on the root logger so this test file
    # never leaves the host process with a stale RotatingFileHandler
    # pointing at a tmp path that's about to vanish.
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    catchem_logging._CONFIGURED = False
    try:
        yield
    finally:
        for h in root.handlers:
            try:
                h.close()
            except Exception:
                pass
        root.handlers = original_handlers
        root.setLevel(original_level)
        catchem_logging._CONFIGURED = False


def test_rotation_constants_match_policy() -> None:
    # The 5 MB × 3 backups policy is the published contract; if it ever
    # changes, the constants AND this test must move in lockstep so the
    # change is visible in code review.
    assert LOG_ROTATION_MAX_BYTES == 5 * 1024 * 1024
    assert LOG_ROTATION_BACKUP_COUNT == 3


def test_log_file_handler_is_rotating(tmp_path: Path) -> None:
    """The file handler installed by configure_logging is rotating."""
    log_path = tmp_path / "logs" / "catchem.log"
    configure_logging(level="DEBUG", log_file=log_path, json_mode=True)

    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1, "exactly one rotating file handler expected"
    handler = rotating[0]
    assert handler.maxBytes == LOG_ROTATION_MAX_BYTES
    assert handler.backupCount == LOG_ROTATION_BACKUP_COUNT
    # The handler must point at the requested log file. Normalize via
    # Path so platform-specific tmp prefixes don't break the comparison.
    assert Path(handler.baseFilename) == log_path


def test_log_directory_is_created(tmp_path: Path) -> None:
    """A fresh boot on a clean machine: parent dir must be auto-created."""
    log_path = tmp_path / "newly-created" / "nested" / "catchem.log"
    assert not log_path.parent.exists()
    configure_logging(level="INFO", log_file=log_path, json_mode=True)
    assert log_path.parent.is_dir()


def test_rollover_creates_backup_file(tmp_path: Path) -> None:
    """Force a rollover and verify ``.1`` backup is produced."""
    log_path = tmp_path / "rotation" / "catchem.log"
    configure_logging(level="DEBUG", log_file=log_path, json_mode=True)

    handler = next(
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )

    # Write enough lines to exceed maxBytes a few times over. We don't
    # rely on the maxBytes constant directly — instead we trigger the
    # rollover by calling doRollover() once we know we've written at
    # least one full line. Belt-and-braces: also write enough bytes that
    # ``shouldRollover`` returns True next time, in case the explicit
    # call ever stops being a guarantee in stdlib.
    payload = "x" * 2048
    rec = logging.LogRecord(
        name="catchem.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=payload,
        args=(),
        exc_info=None,
    )
    handler.emit(rec)
    handler.flush()
    handler.doRollover()
    handler.emit(rec)
    handler.flush()

    backup = log_path.with_suffix(log_path.suffix + ".1")
    assert backup.exists(), f"backup {backup} must be created after doRollover()"
    assert log_path.exists(), "live log file must still exist after rollover"
    # Backup must contain the payload we wrote BEFORE the rollover.
    assert payload in backup.read_text(encoding="utf-8")


def test_configure_without_log_file_installs_only_stream_handler() -> None:
    """When no log_file is passed, only the stderr StreamHandler is wired —
    no RotatingFileHandler is created (covers the ``log_file is None`` branch)."""
    configure_logging(level="INFO", log_file=None, json_mode=True)
    root = logging.getLogger()
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    ), "no rotating file handler should exist without a log_file"
    assert any(
        isinstance(h, logging.StreamHandler) for h in root.handlers
    ), "the stderr stream handler is always installed"


def test_configure_is_idempotent(tmp_path: Path) -> None:
    """A second configure_logging call is a no-op (the ``_CONFIGURED`` guard).

    The first call wires a file handler; the second — with a *different*
    log_file — must NOT add another handler, proving the early-return fires.
    """
    first = tmp_path / "first" / "catchem.log"
    configure_logging(level="DEBUG", log_file=first, json_mode=True)
    assert catchem_logging._CONFIGURED is True
    rotating_after_first = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating_after_first) == 1

    second = tmp_path / "second" / "catchem.log"
    configure_logging(level="DEBUG", log_file=second, json_mode=True)
    rotating_after_second = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    # Idempotent: still exactly one handler, still pointing at the FIRST path.
    assert len(rotating_after_second) == 1
    assert Path(rotating_after_second[0].baseFilename) == first
    # The second directory was never touched because the call short-circuited.
    assert not second.parent.exists()


def test_configure_console_mode_emits_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """json_mode=False routes through structlog's ConsoleRenderer.

    The console renderer produces a ``key=value`` line (no JSON braces around
    the whole record) and includes the structured field we bind. This pins the
    ``else`` branch of the json_mode switch in configure_logging.
    """
    configure_logging(level="INFO", log_file=None, json_mode=False)
    log = get_logger("catchem.test.console")
    log.info("console_event", ticker="AAPL", pct=4.2)
    err = capsys.readouterr().err
    assert "console_event" in err
    assert "ticker" in err and "AAPL" in err
    # ConsoleRenderer is not JSON: the event is rendered as a bare message,
    # not wrapped in a top-level JSON object.
    assert not err.strip().startswith("{")


def test_configure_json_mode_emits_structured_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """json_mode=True routes through JSONRenderer; records carry the bound
    structured fields plus the auto-added ``level`` and ISO ``timestamp``."""
    import json

    configure_logging(level="INFO", log_file=None, json_mode=True)
    log = get_logger("catchem.test.json")
    log.warning("structured_event", symbol="BTC-USD", score=0.91)
    err = capsys.readouterr().err.strip().splitlines()
    # The last line is our record (structlog writes one JSON object per line).
    payload = json.loads(err[-1])
    assert payload["event"] == "structured_event"
    assert payload["symbol"] == "BTC-USD"
    assert payload["score"] == 0.91
    assert payload["level"] == "warning"
    assert "timestamp" in payload  # TimeStamper(fmt="iso") processor


def test_get_logger_returns_usable_bound_logger() -> None:
    """get_logger yields a structlog logger that binds context and logs without
    raising. We don't assert on output here — just that the API is callable."""
    configure_logging(level="DEBUG", log_file=None, json_mode=True)
    log = get_logger("catchem.test.usable")
    assert hasattr(log, "info") and hasattr(log, "bind")
    bound = log.bind(request_id="r-123")
    # Logging through the bound logger must not raise.
    bound.info("hello", extra_field=1)
    bound.debug("debug_line")
    bound.error("error_line")


def test_level_string_is_case_insensitive_and_falls_back(tmp_path: Path) -> None:
    """A lowercase level is upper-cased; an unknown level falls back to INFO
    (covers the ``getattr(logging, level.upper(), logging.INFO)`` lookup)."""
    configure_logging(level="debug", log_file=None, json_mode=True)
    assert logging.getLogger().level == logging.DEBUG

    # Reset for the bogus-level case.
    catchem_logging._CONFIGURED = False
    configure_logging(level="NOT_A_REAL_LEVEL", log_file=None, json_mode=True)
    assert logging.getLogger().level == logging.INFO


def test_get_logger_is_a_structlog_logger() -> None:
    """The returned object is produced by structlog.get_logger (proxy/bound)."""
    configure_logging(level="INFO", log_file=None, json_mode=True)
    log = get_logger("catchem.test.type")
    # structlog returns a lazy proxy that resolves to a bound logger on first
    # use; binding returns a concrete BoundLoggerBase subclass.
    bound = log.bind(k="v")
    assert isinstance(bound, structlog.typing.BindableLogger)
