"""Structured logging.

Every line is a single JSON object on stdout (12-factor: logs are an event
stream, the platform does the routing).  The current request id and GitHub
delivery id ride along in contextvars so they appear on every line emitted
while handling a request without threading them through call signatures.

Secrets never reach a log record: :func:`redact` is the only way values like
tokens and signatures are rendered.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
delivery_id_ctx: ContextVar[str | None] = ContextVar("delivery_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn attaches an ANSI-escaped duplicate of its own message; it would
    # otherwise ride along on every startup line as unreadable control codes.
    "color_message",
}


def redact(value: str | None, keep: int = 0) -> str:
    """Render a secret safely.  Never returns the secret itself."""
    if not value:
        return "<unset>"
    if keep > 0 and len(value) > keep:
        return f"<redacted:{len(value)}chars:...{value[-keep:]}>"
    return f"<redacted:{len(value)}chars>"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (rid := request_id_ctx.get()) is not None:
            payload["request_id"] = rid
        if (did := delivery_id_ctx.get()) is not None:
            payload["delivery_id"] = did
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Human-friendly fallback for local development (LOG_FORMAT=text)."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        }
        if rid := request_id_ctx.get():
            extras["request_id"] = rid
        if did := delivery_id_ctx.get():
            extras["delivery_id"] = did
        suffix = " " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        base = f"{record.levelname:<7} {record.name} :: {record.getMessage()}{suffix}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler on the root logger (idempotent)."""
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own colourised handlers; make them use ours instead
    # so the output stream stays uniformly parseable.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # We emit our own access log line from the middleware.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
