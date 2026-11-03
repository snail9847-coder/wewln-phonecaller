"""Structured logging with correlation IDs. Phone numbers and secrets are always masked."""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .redact import redact, redact_value

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
call_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("call_id", default=None)

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, __file__, 0, "", None, None))
) | {"message", "asctime", "taskName"}
_OWN_HANDLER = "_wewln_handler"
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 5


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, correlation IDs and extra fields."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage(), self._secrets),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        call_id = call_id_var.get()
        if call_id:
            payload["call_id"] = call_id
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = redact_value(value, self._secrets)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info), self._secrets)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable lines for the console, masked the same way as JSON."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        request_id = request_id_var.get()
        if request_id:
            text += f" [request_id={request_id}]"
        return redact(text, self._secrets)


def setup_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    log_file: Path | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Configure the root logger. Safe to call again: previous handlers of ours are replaced."""
    shutdown_logging()
    secrets = tuple(secrets)
    root = logging.getLogger()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(JsonFormatter(secrets) if fmt == "json" else TextFormatter(secrets))
    setattr(console, _OWN_HANDLER, True)
    root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=True
        )
        file_handler.setFormatter(JsonFormatter(secrets))
        setattr(file_handler, _OWN_HANDLER, True)
        root.addHandler(file_handler)
    root.setLevel(level)


def shutdown_logging() -> None:
    """Remove and close our handlers (releases log files, which matters on Windows)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _OWN_HANDLER, False):
            root.removeHandler(handler)
            handler.close()
