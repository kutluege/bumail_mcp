"""Organize tools: postalari_isaretle, postalari_tasi, cope_at, arsivle.

Nothing here can permanently delete mail: "trash" is a move to the Trash folder, and moves never
issue a plain EXPUNGE (see posta_mcp.imap.Mailbox.move).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from posta_mcp.server import Deps
from posta_mcp.tools._common import WRITE, with_mailbox

MAX_UIDS = 500


def check_uids(uids: list[int]) -> list[int]:
    clean = sorted({int(u) for u in uids})
    if not clean:
        raise ToolError("Give at least one message UID.")
    if clean[0] < 1:
        raise ToolError("UIDs must be positive integers.")
    if len(clean) > MAX_UIDS:
        raise ToolError(f"Too many messages at once (max {MAX_UIDS}).")
    return clean


def _count(n: int) -> str:
    return f"{n} message{'s' if n != 1 else ''}"


def register(server: MCPServer, deps: Deps) -> None:
    @server.tool(name="postalari_isaretle", annotations=WRITE)
    def mark_messages(
        folder: str, uids: list[int], seen: bool | None = None, flagged: bool | None = None
    ) -> str:
        """Set or clear the read (seen) and/or starred (flagged) state of messages by UID."""
        ids = check_uids(uids)
        if seen is None and flagged is None:
            raise ToolError("Set 'seen' and/or 'flagged' to true or false.")
        with_mailbox(
            deps,
            "Marking messages",
            lambda mb: mb.set_flags(folder, ids, seen=seen, flagged=flagged),
        )
        changes = [f"{n}={v}" for n, v in (("seen", seen), ("flagged", flagged)) if v is not None]
        return f"Updated {_count(len(ids))} in {folder}: {', '.join(changes)}."

    @server.tool(name="postalari_tasi", annotations=WRITE)
    def move_messages(folder: str, uids: list[int], destination: str) -> str:
        """Move messages (by UID) to another existing folder."""
        ids = check_uids(uids)
        with_mailbox(deps, "Moving messages", lambda mb: mb.move(folder, ids, destination))
        return f"Moved {_count(len(ids))} from {folder} to {destination}."

    @server.tool(name="cope_at", annotations=WRITE)
    def trash_messages(folder: str, uids: list[int]) -> str:
        """Move messages to the Trash folder (recoverable; nothing is permanently deleted)."""
        ids = check_uids(uids)

        def run(mb) -> str:
            trash = mb.ensure_special_folder("trash")
            if trash == folder:
                raise ToolError("Those messages are already in Trash; nothing is deleted.")
            mb.move(folder, ids, trash)
            return trash

        trash = with_mailbox(deps, "Trashing messages", run)
        return f"Moved {_count(len(ids))} from {folder} to {trash}."

    @server.tool(name="arsivle", annotations=WRITE)
    def archive_messages(folder: str, uids: list[int]) -> str:
        """Move messages to the Archive folder."""
        ids = check_uids(uids)

        def run(mb) -> str:
            archive = mb.ensure_special_folder("archive")
            if archive == folder:
                raise ToolError("Those messages are already in Archive.")
            mb.move(folder, ids, archive)
            return archive

        archive = with_mailbox(deps, "Archiving messages", run)
        return f"Moved {_count(len(ids))} from {folder} to {archive}."
