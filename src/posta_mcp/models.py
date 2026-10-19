"""Message models shared by the IMAP layer, the tools and the composer.

All models are pydantic ``BaseModel`` s; tools render them as text through ``untrusted``.
Every text field here is *untrusted* email data; wrap it with ``untrusted`` before showing
it to a model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MessageSummary(BaseModel):
    """Header-level view of one message (what a search / list returns)."""

    uid: int
    folder: str
    from_name: str = ""
    from_addr: str = ""
    to: list[str] = Field(default_factory=list)  # "Name <addr>" or "addr"
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    date: str | None = None  # ISO 8601, None when missing/unparseable
    flags: list[str] = Field(default_factory=list)
    size: int | None = None
    has_attachments: bool = False
    list_id: str | None = None
    message_id: str | None = None

    @classmethod
    def from_headers(
        cls,
        headers: bytes,
        *,
        uid: int,
        folder: str,
        flags: Any = (),
        size: int | None = None,
        has_attachments: bool | None = None,
    ) -> MessageSummary:
        """Build a summary from raw header bytes (``BODY.PEEK[HEADER.FIELDS (...)]``).
        Without BODYSTRUCTURE, ``has_attachments`` is guessed from
        ``Content-Type: multipart/mixed`` unless given explicitly."""
        from .mime import header_fields, parse_header_bytes

        msg = parse_header_bytes(headers)
        fields = header_fields(msg)
        if has_attachments is None:
            has_attachments = msg.get_content_type() == "multipart/mixed"
        return cls(
            uid=uid,
            folder=folder,
            flags=normalize_flags(flags),
            size=size,
            has_attachments=has_attachments,
            **fields,
        )


class Attachment(BaseModel):
    filename: str
    content_type: str
    size: int
    index: int  # 0-based position among the message's attachments


class ParsedMessage(MessageSummary):
    """Full message: summary fields plus bodies, attachments and threading headers."""

    reply_to: list[str] = Field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)  # message-ids, oldest first
    text_body: str = ""  # readable body parts in order (text/plain preferred within alternatives)
    attachments: list[Attachment] = Field(default_factory=list)

    @property
    def body(self) -> str:
        """Readable body text (HTML parts already rendered to text)."""
        return self.text_body


def normalize_flags(flags: Any) -> list[str]:
    """IMAP flags arrive as bytes or str (e.g. ``b'\\\\Seen'``); return de-duplicated str list."""
    out: list[str] = []
    for f in flags or ():
        s = f.decode("ascii", "replace") if isinstance(f, bytes) else str(f)
        if s not in out:
            out.append(s)
    return out
