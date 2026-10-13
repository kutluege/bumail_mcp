"""Logging: stderr only (stdout belongs to the MCP protocol), plus secret redaction."""

from __future__ import annotations

import logging
import os
import sys

LOGGER_NAME = "posta_mcp"
REDACTED = "***"

_secrets: set[str] = set()


def register_secret(secret: str) -> None:
    """Remember a secret so it is scrubbed from every log record and from `redact`."""
    if secret:
        _secrets.add(secret)


def redact(text: str, *extra: str) -> str:
    """Replace every known secret (and any `extra` ones) in `text`."""
    for secret in (*_secrets, *extra):
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:  # tracebacks may echo secrets too
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _level() -> int:
    name = os.environ.get("POSTA_LOG_LEVEL", "WARNING").strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.WARNING


def configure_logging() -> logging.Logger:
    """Send the package logger (and the root logger, which the MCP SDK uses) to stderr only."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactFilter())

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers[:] = [handler]
    logger.setLevel(_level())
    logger.propagate = False

    # The SDK calls logging.basicConfig(); a root handler present first makes that a no-op,
    # so nothing can end up on stdout.
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(_level())
    return logger
