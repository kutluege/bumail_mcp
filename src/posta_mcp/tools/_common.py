"""Helpers shared by tool modules.

Tools are plain sync functions: the SDK runs them in a worker thread, so blocking IMAP/SMTP
calls are fine. Errors reaching the client must be short and contain no secrets.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from posta_mcp.logs import redact

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_LIMIT = 100

# Changes mailbox state but nothing is lost (flags, moves to Trash, drafts).
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Leaves the user's machine and cannot be undone (sending mail).
SEND = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


@contextmanager
def tool_errors(action: str) -> Iterator[None]:
    """Turn any failure into a ToolError with a short, redacted message (no bodies, no secrets)."""
    try:
        yield
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - every error must be mapped for the client
        log.warning("%s failed: %s", action, type(exc).__name__)
        raise ToolError(f"{action} failed: {redact(str(exc))[:300]}") from None


def clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def with_mailbox(deps, action: str, fn: Callable[..., T]) -> T:
    """Open a mailbox, run `fn(mailbox)`, map errors."""
    with tool_errors(action), deps.mailbox() as mb:
        return fn(mb)
