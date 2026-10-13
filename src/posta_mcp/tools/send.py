"""Gated sending: ``send_message`` and ``send_draft``.

Safety design (SPEC non-negotiable #4)
--------------------------------------
Order of checks, enforced by the *resolver* that runs before the tool body:

1. Sending enabled?  (``config.send_enabled``: ``[sending] enabled`` or ``POSTA_ALLOW_SEND``.)
   If not, refuse and tell the user how to enable it themselves.  No tool can enable sending.
2. Does the client support form elicitation?  If not, refuse (suggest a draft).
3. Build the exact message (for ``send_draft``: fetch the draft).
4. Ask the human to confirm (``Annotated[ElicitationResult[Confirm], Resolve(fn)]``: works on
   both the handshake and the modern protocol era, unlike plain ``ctx.elicit``).
5. Tool body: send only on ``accept`` with ``confirm == True``.

Binding the confirmation to the bytes that are sent
---------------------------------------------------
The resolver and the tool body are separate calls and the resolver may even re-run in several
protocol rounds, so building the message twice would yield different ``Date`` / ``Message-ID``
headers.  Instead the message is built **once** and parked in a small in-memory store
(:class:`_Store`) keyed by a hash of the tool arguments (for drafts: of the draft's current raw
bytes).  The resolver gets-or-creates the entry, so every round shows the same message; the tool
body *never builds anything*: it pops the entry by the same key (a miss means "no confirmation on
record" and nothing is sent).  When the entry is created we compute

* ``wire`` = ``compose.send_bytes(msg)`` (the bytes that will go to SMTP DATA, no Bcc header),
* ``wire_sha`` = SHA-256 of ``wire``, printed in the confirmation text ("fingerprint"),
* ``binding`` = SHA-256 over sender + envelope recipients (To/Cc/Bcc) + ``wire``.

Just before sending, the body recomputes both digests from the stored message object and
requires them to match what was computed before the question was asked; the bytes then handed
to SMTP are the stored ``wire`` itself.  Entries expire after 30 minutes and are consumed by the
first body that uses them, so one confirmation sends once.  A model cannot supply the decision:
``decision`` is a resolver-filled parameter, not part of the tool's input schema, and a
``confirm`` argument is not a parameter at all.
"""

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from posta_mcp import compose, smtp
from posta_mcp.logs import redact
from posta_mcp.tools._common import SEND, tool_errors

log = logging.getLogger(__name__)

MAX_RECIPIENTS = 50  # guard against mass mailing by a confused or manipulated model
BODY_PREVIEW_CHARS = 4000
PENDING_TTL_SECONDS = 30 * 60

DISABLED_MESSAGE = (
    "Sending is disabled, so nothing was sent. Only the user can turn it on: set "
    "`enabled = true` under `[sending]` in posta-mcp's config.toml, or set the environment "
    "variable POSTA_ALLOW_SEND=1 for the MCP server, then restart it. This assistant cannot "
    "enable sending. Meanwhile, use create_draft (or create_reply_draft) to save the message "
    "in Drafts so the user can review and send it from their mail client."
)
NO_ELICITATION_MESSAGE = (
    "This MCP client cannot ask the user for confirmation (no form elicitation support), so "
    "the message cannot be confirmed with the user and was NOT sent. Create a draft instead "
    "(create_draft or create_reply_draft); the user can send it from their mail client."
)


class Confirm(BaseModel):
    confirm: bool = Field(
        default=False,
        title="Send this exact message",
        description="Set to true to send exactly the message shown above. Leave false to cancel.",
    )


class SendResult(BaseModel):
    status: Literal["sent", "not_sent"]
    detail: str
    recipients: list[str] = Field(default_factory=list)
    refused_recipients: list[str] = Field(default_factory=list)
    saved_to_sent: bool = False
    draft_moved_to_trash: bool = False
    warnings: list[str] = Field(default_factory=list)


# --- the parked, hash-bound message -------------------------------------------------------


def _sha256(*parts: bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
        h.update(b"\x00")
    return h.hexdigest()


def _binding(sender: str, recipients: tuple[str, ...], wire: bytes) -> str:
    return _sha256(sender.encode(), "\n".join(recipients).encode(), wire)


@dataclass(frozen=True)
class Prepared:
    msg: EmailMessage
    sender: str
    recipients: tuple[str, ...]
    wire: bytes
    wire_sha: str  # shown to the user
    binding: str  # sender + envelope + wire
    shown: str  # the exact confirmation text
    expires: float
    draft: tuple[str, int] | None = None  # (folder, uid) when sending a stored draft

    def still_matches(self) -> bool:
        """Re-derive the digests from the stored message and compare with the pre-ask values."""
        wire = compose.send_bytes(self.msg)
        recipients = tuple(compose.envelope_recipients(self.msg))
        return (
            wire == self.wire
            and hashlib.sha256(wire).hexdigest() == self.wire_sha
            and recipients == self.recipients
            and _binding(self.sender, recipients, wire) == self.binding
        )


class _Store:
    def __init__(self) -> None:
        self._items: dict[str, Prepared] = {}
        self._lock = threading.Lock()

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, p in self._items.items() if p.expires <= now]:
            del self._items[key]

    def get_or_create(self, key: str, make: Any) -> Prepared:
        with self._lock:
            self._sweep()
            if key not in self._items:
                self._items[key] = make()
            return self._items[key]

    def pop(self, key: str) -> Prepared | None:
        with self._lock:
            self._sweep()
            return self._items.pop(key, None)


# --- confirmation text --------------------------------------------------------------------


def _header(msg: EmailMessage, name: str) -> str:
    values = [str(v) for v in msg.get_all(name, [])]
    return "; ".join(values)


def _text_of(part: EmailMessage) -> str:
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001 - odd charsets etc.; fall back to a lossy decode
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", "replace")
    return text.replace("\r\n", "\n")


def _preview(text: str) -> str:
    if len(text) <= BODY_PREVIEW_CHARS:
        return text
    return (
        f"{text[:BODY_PREVIEW_CHARS]}\n[... only the first {BODY_PREVIEW_CHARS} of {len(text)} "
        f"characters are shown here; the complete text will be sent ...]"
    )


def _describe_parts(msg: EmailMessage) -> tuple[list[str], list[str]]:
    """(text part blocks, attachment descriptions) for the confirmation."""
    blocks: list[str] = []
    attachments: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if ctype.startswith("text/") and disposition != "attachment" and not filename:
            label = "body" if ctype == "text/plain" else f"{ctype} part (source)"
            blocks.append(f"----- {label} -----\n{_preview(_text_of(part))}\n----- end -----")
        else:
            size = len(part.get_payload(decode=True) or b"")
            attachments.append(f"{filename or '(unnamed)'} [{ctype}, {size} bytes]")
    return blocks, attachments


def _confirmation_text(msg: EmailMessage, recipients: tuple[str, ...], wire_sha: str) -> str:
    blocks, attachments = _describe_parts(msg)
    lines = [
        "Review this email. It is sent only if you set the checkbox below to true and accept.",
        "",
        f"From: {_header(msg, 'From')}",
        f"To: {_header(msg, 'To') or '(none)'}",
        f"Cc: {_header(msg, 'Cc') or '(none)'}",
        f"Bcc: {_header(msg, 'Bcc') or '(none)'}",
        f"Subject: {_header(msg, 'Subject')}",
        f"Attachments: {', '.join(attachments) if attachments else 'none'}",
        f"Will be delivered to ({len(recipients)}): {', '.join(recipients)}",
        f"Fingerprint (SHA-256 of the exact bytes to be sent): {wire_sha}",
        "",
        *(blocks or ["(no text body)"]),
    ]
    return "\n".join(lines)


def _prepare(msg: EmailMessage, sender: str, *, draft: tuple[str, int] | None = None) -> Prepared:
    recipients = tuple(compose.envelope_recipients(msg))
    if not recipients:
        raise ToolError("The message has no recipients; nothing was sent.")
    if len(recipients) > MAX_RECIPIENTS:
        raise ToolError(f"Refusing to send to more than {MAX_RECIPIENTS} recipients at once.")
    wire = compose.send_bytes(msg)
    wire_sha = hashlib.sha256(wire).hexdigest()
    return Prepared(
        msg=msg,
        sender=sender,
        recipients=recipients,
        wire=wire,
        wire_sha=wire_sha,
        binding=_binding(sender, recipients, wire),
        shown=_confirmation_text(msg, recipients, wire_sha),
        expires=time.monotonic() + PENDING_TTL_SECONDS,
        draft=draft,
    )


# --- checks shared by both tools -----------------------------------------------------------


def _can_elicit(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return bool(caps and caps.elicitation and caps.elicitation.form is not None)


def _failed(exc: Exception) -> ToolError:
    return ToolError(redact(str(exc))[:300])


def register(server: MCPServer, deps: Any) -> None:
    config = deps.config
    store = _Store()

    def gate(ctx: Context) -> None:
        """Checks (a) and (b): raise ToolError with a refusal before anything else happens."""
        if not config.send_enabled:
            raise ToolError(DISABLED_MESSAGE)
        if not _can_elicit(ctx):
            raise ToolError(NO_ELICITATION_MESSAGE)

    def message_key(**args: Any) -> str:
        return _sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode())

    def build_new(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str],
        bcc: list[str],
        reply_to_folder: str | None,
        reply_to_uid: int | None,
        reply_all: bool,
    ) -> EmailMessage:
        if (reply_to_folder is None) != (reply_to_uid is None):
            raise ToolError("reply_to_folder and reply_to_uid must be given together.")
        try:
            if reply_to_uid is None or reply_to_folder is None:
                return compose.build_message(config.email, to, subject, body, cc, bcc)
            with tool_errors("Reading the message being replied to"), deps.mailbox() as mb:
                original = mb.fetch(reply_to_folder, reply_to_uid)
            draft = compose.build_reply(original, config.email, body, reply_all)
            # Explicit arguments win over the reply defaults.
            return compose.build_message(
                config.email,
                to or [str(a) for a in draft.get_all("To", [])],
                subject or str(draft["Subject"]),
                draft.get_content(),
                cc or [str(a) for a in draft.get_all("Cc", [])],
                bcc,
                in_reply_to=draft["In-Reply-To"],
                references=draft["References"],
            )
        except ValueError as exc:
            raise ToolError(f"Invalid message: {exc}") from None

    def confirm_message(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str],
        bcc: list[str],
        reply_to_folder: str | None,
        reply_to_uid: int | None,
        reply_all: bool,
        ctx: Context,
    ) -> Elicit[Confirm]:
        gate(ctx)
        key = message_key(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_folder=reply_to_folder,
            reply_to_uid=reply_to_uid,
            reply_all=reply_all,
        )
        prepared = store.get_or_create(
            key,
            lambda: _prepare(
                build_new(to, subject, body, cc, bcc, reply_to_folder, reply_to_uid, reply_all),
                config.email,
            ),
        )
        return Elicit(prepared.shown, Confirm)

    def load_draft(uid: int) -> tuple[str, bytes]:
        with tool_errors("Reading the draft"), deps.mailbox() as mb:
            folder = mb.special_folder("drafts")
            return folder, mb.fetch_raw(folder, uid)

    def draft_key(folder: str, uid: int, raw: bytes) -> str:
        return _sha256(b"draft", folder.encode(), str(uid).encode(), raw)

    def build_from_draft(folder: str, uid: int, raw: bytes) -> Prepared:
        msg = message_from_bytes(raw, policy=policy.SMTP)
        if not isinstance(msg, EmailMessage):
            raise ToolError("The draft could not be parsed.")
        try:
            sender = compose.parse_address(_header(msg, "From"))
        except ValueError:
            raise ToolError("The draft has no single valid From address; not sent.") from None
        if f"{sender.username}@{sender.domain}".lower() != config.email.lower():
            raise ToolError(
                "The draft's From address is not this account's address; refusing to send it."
            )
        return _prepare(msg, config.email, draft=(folder, uid))

    def confirm_draft(uid: int, ctx: Context) -> Elicit[Confirm]:
        gate(ctx)
        folder, raw = load_draft(uid)
        prepared = store.get_or_create(
            draft_key(folder, uid, raw), lambda: build_from_draft(folder, uid, raw)
        )
        return Elicit(prepared.shown, Confirm)

    def conclude(
        prepared: Prepared | None, decision: ElicitationResult[Confirm], missing: str
    ) -> SendResult:
        """Common tail of both tools: send only on accept + confirm=True of a stored message."""
        if decision.action == "decline":
            return SendResult(
                status="not_sent", detail="NOT SENT: the user declined to send this message."
            )
        if decision.action != "accept":
            return SendResult(
                status="not_sent", detail="NOT SENT: the user cancelled the confirmation."
            )
        if decision.data.confirm is not True:
            return SendResult(
                status="not_sent",
                detail="NOT SENT: the confirmation was accepted but the send checkbox was not "
                "set to true.",
            )
        if prepared is None:
            raise ToolError(missing)
        return dispatch(prepared)

    def dispatch(prepared: Prepared) -> SendResult:
        if not prepared.still_matches():
            raise ToolError("The message changed after it was shown to the user; NOT sent.")
        try:
            refused = smtp.send_raw(
                config, prepared.sender, list(prepared.recipients), prepared.wire
            )
        except smtp.SmtpError as exc:
            raise ToolError(f"Sending failed; nothing was sent: {exc}") from None
        log.info("sent a message to %d recipients", len(prepared.recipients))
        result = SendResult(
            status="sent",
            detail=f"Sent to {len(prepared.recipients) - len(refused)} recipient(s).",
            recipients=list(prepared.recipients),
            refused_recipients=refused,
        )
        if refused:
            result.warnings.append(
                "The server refused some recipients (they did not get the message): "
                + ", ".join(refused)
            )
        _after_send(prepared, result)
        return result

    def _after_send(prepared: Prepared, result: SendResult) -> None:
        """Best effort: a failure here must not look like the send failed (it did not)."""
        save = config.save_sent != "never"
        if not save and prepared.draft is None:
            return
        try:
            with deps.mailbox() as mb:
                if save:
                    try:
                        mb.append(
                            mb.special_folder("sent"),
                            compose.message_bytes(prepared.msg),
                            (r"\Seen",),
                        )
                        result.saved_to_sent = True
                    except Exception as exc:  # noqa: BLE001
                        result.warnings.append(
                            f"Sent, but saving a copy to Sent failed: {redact(str(exc))[:200]}"
                        )
                if prepared.draft is not None:
                    folder, uid = prepared.draft
                    try:
                        mb.move(folder, [uid], mb.special_folder("trash"))
                        result.draft_moved_to_trash = True
                    except Exception as exc:  # noqa: BLE001
                        result.warnings.append(
                            f"Sent, but moving the draft to Trash failed: {redact(str(exc))[:200]}"
                        )
        except Exception as exc:  # noqa: BLE001 - could not even connect
            result.warnings.append(
                f"Sent, but the mailbox could not be updated: {redact(str(exc))[:200]}"
            )

    @server.tool(annotations=SEND)
    def send_message(
        to: list[str],
        subject: str,
        body: str,
        decision: Annotated[ElicitationResult[Confirm], Resolve(confirm_message)],
        cc: list[str] = [],  # noqa: B006 - never mutated; the schema default is what matters
        bcc: list[str] = [],  # noqa: B006
        reply_to_folder: str | None = None,
        reply_to_uid: int | None = None,
        reply_all: bool = False,
    ) -> SendResult:
        """Send an email. DISABLED unless the user enabled sending, and the user must confirm
        every message in a dialog that shows the exact recipients and text. Nothing is sent
        if they decline. To reply, pass reply_to_folder and reply_to_uid (threading headers
        and, when `to`/`subject` are empty, recipients and subject come from that message).
        Prefer create_draft when unsure."""
        if not config.send_enabled:  # defence in depth: the resolver already checked
            raise ToolError(DISABLED_MESSAGE)
        key = message_key(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_folder=reply_to_folder,
            reply_to_uid=reply_to_uid,
            reply_all=reply_all,
        )
        return conclude(
            store.pop(key),
            decision,
            "No confirmed message is on record (expired?); NOT sent. Try again.",
        )

    @server.tool(annotations=SEND)
    def send_draft(
        uid: int,
        decision: Annotated[ElicitationResult[Confirm], Resolve(confirm_draft)],
    ) -> SendResult:
        """Send a message from the Drafts folder (by UID). DISABLED unless the user enabled
        sending, and the user must confirm in a dialog showing the exact message. After a
        successful send the draft is moved to Trash (not deleted)."""
        if not config.send_enabled:
            raise ToolError(DISABLED_MESSAGE)
        folder, raw = load_draft(uid)  # the draft must still be exactly what was shown
        return conclude(
            store.pop(draft_key(folder, uid, raw)),
            decision,
            "The draft changed or the confirmation expired; NOT sent. Try again.",
        )
