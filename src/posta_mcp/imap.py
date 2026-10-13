"""IMAP access. Every message operation is by UID; bodies are fetched with BODY.PEEK.

Interface contract shared by the read/organize/drafts/send/digest tool modules. The method
bodies are implemented in M1/M2; signatures here are the agreement between modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from posta_mcp.config import Config
from posta_mcp.models import MessageSummary, ParsedMessage

SpecialRole = Literal["sent", "drafts", "trash", "archive"]


@dataclass(frozen=True)
class SearchCriteria:
    """All set fields are ANDed. Strings are matched by the server (IMAP SEARCH, CHARSET UTF-8)."""

    from_: str | None = None
    to: str | None = None
    subject: str | None = None
    text: str | None = None
    since: date | None = None
    before: date | None = None
    unseen: bool | None = None
    flagged: bool | None = None


@dataclass(frozen=True)
class FolderInfo:
    name: str
    flags: tuple[str, ...]
    special_use: SpecialRole | None


class MailboxError(RuntimeError):
    """Raised with a short, secret-free message suitable for showing to the user."""


class Mailbox:
    """A logged-in IMAP session. Use as a context manager: connect on enter, log out on exit."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def __enter__(self) -> Mailbox:
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:
        raise NotImplementedError

    def list_folders(self) -> list[FolderInfo]:
        raise NotImplementedError

    def special_folder(self, role: SpecialRole) -> str:
        """SPECIAL-USE first, then config/preset folder names; MailboxError if none exists."""
        raise NotImplementedError

    def search(
        self, folder: str, criteria: SearchCriteria, limit: int = 20
    ) -> list[MessageSummary]:
        """Newest first, at most `limit` (header data only, PEEK)."""
        raise NotImplementedError

    def fetch(self, folder: str, uid: int, mark_seen: bool = False) -> ParsedMessage:
        """Full message via BODY.PEEK[]; sets \\Seen only if `mark_seen`."""
        raise NotImplementedError

    def fetch_raw(self, folder: str, uid: int) -> bytes:
        """Raw RFC 822 bytes via BODY.PEEK[] (for drafts, attachments)."""
        raise NotImplementedError

    def set_flags(
        self,
        folder: str,
        uids: list[int],
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        raise NotImplementedError

    def move(self, folder: str, uids: list[int], destination: str) -> None:
        """UID MOVE, else UID COPY + \\Deleted + UID EXPUNGE <uids> (UIDPLUS), else refuse.

        Never a plain EXPUNGE.
        """
        raise NotImplementedError

    def append(self, folder: str, raw: bytes, flags: tuple[str, ...] = ()) -> int | None:
        """APPEND; returns the new UID when the server reports APPENDUID."""
        raise NotImplementedError
