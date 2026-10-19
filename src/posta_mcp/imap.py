"""IMAP access. Every message operation is by UID; bodies are fetched with BODY.PEEK.

Interface contract shared by the read/organize/drafts/send/digest tool modules.

Safety properties (see SPEC section 1):

* TLS is always verified (`tls.make_ssl_context`); `ssl` is implicit TLS, `starttls` upgrades
  *before* login and refuses to log in when STARTTLS is missing or fails. No plaintext path.
* Message data is fetched only with ``BODY.PEEK[...]``; ``\\Seen`` changes only via ``set_flags``.
* Nothing is ever permanently deleted: no plain ``EXPUNGE`` and no ``CLOSE`` (which silently
  expunges ``\\Deleted`` messages) is issued. Moves use ``UID MOVE`` or ``UID COPY`` +
  ``\\Deleted`` + ``UID EXPUNGE <exactly those uids>`` (UIDPLUS); otherwise we refuse.
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import imapclient
from imapclient import exceptions as imap_exc

from posta_mcp import credentials, hints, tls
from posta_mcp.config import MAX_MESSAGE_BYTES, Config
from posta_mcp.logs import redact
from posta_mcp.mime import parse_message
from posta_mcp.models import MessageSummary, ParsedMessage, normalize_flags

log = logging.getLogger(__name__)

# imaplib (driven by imapclient) logs every protocol line, including message bodies, at DEBUG.
# Message content must never be logged (non-negotiable 7), whatever POSTA_LOG_LEVEL says.
for _name in ("imapclient", "imaplib"):  # imapclient.imaplib is a child of the first
    logging.getLogger(_name).setLevel(logging.WARNING)

SpecialRole = Literal["sent", "drafts", "trash", "archive"]

_SPECIAL_FLAGS: dict[str, SpecialRole] = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\trash": "trash",
    "\\archive": "archive",
}
_SEEN = b"\\Seen"
_FLAGGED = b"\\Flagged"
_DELETED = b"\\Deleted"
_HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID LIST-ID CONTENT-TYPE"
_HEADER_KEY = f"BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})]"
_APPENDUID = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")
_FETCH_BATCH = 200


@dataclass(frozen=True)
class SearchCriteria:
    """All set fields are ANDed. Strings are matched by the server (IMAP SEARCH; CHARSET UTF-8
    when a value is not ASCII)."""

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
    """Raised with a short, secret-free message suitable for showing to the user.

    `kind` classifies the failure for ``posta_mcp.hints`` (None: no specific advice).
    """

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


ClientFactory = Callable[..., Any]


def _new_client(host: str, port: int, *, ssl_on: bool, context: ssl.SSLContext, timeout: float):
    return imapclient.IMAPClient(
        host, port=port, ssl=ssl_on, ssl_context=context, use_uid=True, timeout=timeout
    )


def _describe(exc: BaseException) -> str:
    """Short, secret-free description of a low-level failure."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"TLS certificate verification failed ({exc.verify_message})"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error ({exc.reason or type(exc).__name__})"
    if isinstance(exc, TimeoutError):  # socket.timeout is an alias
        return "timed out"
    if isinstance(exc, OSError):
        return f"network error ({type(exc).__name__})"
    return redact(str(exc)).strip()[:200] or type(exc).__name__


@contextmanager
def _errors(action: str) -> Iterator[None]:
    """Map imapclient/imaplib/socket failures to MailboxError (message redacted)."""
    try:
        yield
    except MailboxError:
        raise
    except (imap_exc.IMAPClientError, imaplib.IMAP4.error, OSError, EOFError) as exc:
        log.warning("IMAP %s failed: %s", action, type(exc).__name__)
        raise MailboxError(
            f"IMAP {action} failed: {_describe(exc)}", kind=hints.classify(exc)
        ) from None


def _walk_has_attachment(node: Any) -> bool:
    """True if a parsed BODYSTRUCTURE contains a part with an `attachment` disposition."""
    if isinstance(node, tuple | list):
        if (
            len(node) == 2
            and isinstance(node[0], bytes | str)
            and (node[0].lower() in (b"attachment", "attachment"))
        ):
            return True
        return any(_walk_has_attachment(n) for n in node)
    return False


def _key(data: dict, prefix: bytes) -> Any:
    """Value of the first FETCH item whose key starts with `prefix` (e.g. b'BODY[HEADER')."""
    for k, v in data.items():
        if isinstance(k, bytes) and k.upper().startswith(prefix):
            return v
    return None


def _flag_bytes(flags: Iterable[str | bytes]) -> list[bytes]:
    return [f if isinstance(f, bytes) else f.encode("ascii") for f in flags]


class Mailbox:
    """A logged-in IMAP session. Use as a context manager: connect on enter, log out on exit."""

    def __init__(
        self, config: Config, *, client_factory: ClientFactory | None = None, timeout: float = 30
    ) -> None:
        self.config = config
        self._factory = client_factory or _new_client
        self._timeout = timeout
        self._client: Any = None
        self._selected: tuple[str, bool] | None = None

    # ------------------------------------------------------------------ connection

    def __enter__(self) -> Mailbox:
        self._connect()
        return self

    def __exit__(self, *exc: object) -> None:
        client, self._client = self._client, None
        self._selected = None
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - best effort; the socket is dropped anyway
                log.debug("logout failed", exc_info=False)

    def _connect(self) -> None:
        cfg = self.config.imap
        if cfg.security not in ("ssl", "starttls"):
            raise MailboxError("Unsupported IMAP security mode; use 'ssl' or 'starttls'.")
        if not cfg.host:
            raise MailboxError("No IMAP host configured.")
        password = credentials.get_password(self.config.login)  # CredentialsError is secret-free
        context = tls.make_ssl_context(self.config.ca_file)
        client = None
        try:
            with _errors(f"connection to {cfg.host}:{cfg.port}"):
                client = self._factory(
                    cfg.host,
                    cfg.port,
                    ssl_on=cfg.security == "ssl",
                    context=context,
                    timeout=self._timeout,
                )
                if cfg.security == "starttls":
                    if not client.has_capability("STARTTLS"):
                        raise MailboxError(
                            "The server does not offer STARTTLS; refusing to log in without "
                            "encryption. Use security 'ssl' or a server that supports STARTTLS.",
                            kind="insecure",
                        )
                    client.starttls(context)  # verified; failure raises, login never happens
            try:
                client.login(self.config.login, password)
            except (imap_exc.IMAPClientError, imaplib.IMAP4.error):
                raise MailboxError(
                    "IMAP login failed: the server rejected the credentials "
                    f"for '{self.config.login}'.",
                    kind="auth",
                ) from None
            except OSError as exc:
                raise MailboxError(
                    f"IMAP login failed: {_describe(exc)}", kind=hints.classify(exc)
                ) from None
        except BaseException:
            if client is not None:
                try:
                    client.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            raise
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            raise MailboxError("Mailbox is not connected (use it as a context manager).")
        return self._client

    def _has(self, capability: str) -> bool:
        return bool(self.client.has_capability(capability))

    def _select(self, folder: str, *, readonly: bool) -> None:
        if self._selected == (folder, readonly):
            return
        self._selected = None
        with _errors(f"opening folder '{folder}'"):
            self.client.select_folder(folder, readonly=readonly)
        self._selected = (folder, readonly)

    # ------------------------------------------------------------------ folders

    def list_folders(self) -> list[FolderInfo]:
        with _errors("folder listing"):
            raw = self.client.list_folders()
        out: list[FolderInfo] = []
        for flags, _delim, name in raw:
            names = tuple(
                f.decode("ascii", "replace") if isinstance(f, bytes) else str(f) for f in flags
            )
            role = next(
                (_SPECIAL_FLAGS[f.lower()] for f in names if f.lower() in _SPECIAL_FLAGS), None
            )
            out.append(FolderInfo(name=str(name), flags=names, special_use=role))
        return out

    def folder_exists(self, name: str) -> bool:
        return any(f.name == name for f in self.list_folders())

    def _find_special(self, role: SpecialRole) -> str | None:
        folders = self.list_folders()
        for f in folders:  # 1. SPECIAL-USE attribute
            if f.special_use == role:
                return f.name
        wanted = getattr(self.config.folders, role)  # 2. configured / preset name
        for f in folders:
            if f.name == wanted:
                return f.name
        lowered = wanted.casefold()
        for f in folders:  # 3. same name ignoring case
            if f.name.casefold() == lowered:
                return f.name
        return None

    def special_folder(self, role: SpecialRole) -> str:
        """SPECIAL-USE first, then config/preset folder names; MailboxError if none exists."""
        found = self._find_special(role)
        if found is None:
            raise MailboxError(
                f"No {role} folder found (looked for '{getattr(self.config.folders, role)}')."
            )
        return found

    def ensure_special_folder(self, role: SpecialRole) -> str:
        """Like `special_folder`, but creates the configured folder when it does not exist.

        Call this only when the user's action needs the folder (saving a draft, trashing,
        archiving).
        """
        found = self._find_special(role)
        if found is not None:
            return found
        name = getattr(self.config.folders, role)
        with _errors(f"creating folder '{name}'"):
            self.client.create_folder(name)
        return name

    # ------------------------------------------------------------------ search / fetch

    @staticmethod
    def _criteria(c: SearchCriteria) -> tuple[list[Any], str | None]:
        parts: list[Any] = []
        for key, value in (
            ("FROM", c.from_),
            ("TO", c.to),
            ("SUBJECT", c.subject),
            ("TEXT", c.text),
        ):
            if value:
                parts += [key, value]
        if c.since:
            parts += ["SINCE", c.since]
        if c.before:
            parts += ["BEFORE", c.before]
        if c.unseen is not None:
            parts.append("UNSEEN" if c.unseen else "SEEN")
        if c.flagged is not None:
            parts.append("FLAGGED" if c.flagged else "UNFLAGGED")
        charset = "UTF-8" if any(isinstance(p, str) and not p.isascii() for p in parts) else None
        return parts or ["ALL"], charset

    def search(
        self, folder: str, criteria: SearchCriteria, limit: int = 20
    ) -> list[MessageSummary]:
        """Newest first (highest UID first), at most `limit` (header data only, PEEK)."""
        limit = max(1, int(limit))
        self._select(folder, readonly=True)
        parts, charset = self._criteria(criteria)
        with _errors("search"):
            found = self.client.search(parts, charset) if charset else self.client.search(parts)
        uids = sorted((int(u) for u in found), reverse=True)[:limit]
        return self.summaries(folder, uids)

    def summaries(self, folder: str, uids: list[int]) -> list[MessageSummary]:
        """Header summaries for the given UIDs, in the given order (missing UIDs are skipped)."""
        if not uids:
            return []
        self._select(folder, readonly=True)
        data: dict[int, dict] = {}
        for i in range(0, len(uids), _FETCH_BATCH):
            with _errors("header fetch"):
                data.update(
                    self.client.fetch(
                        uids[i : i + _FETCH_BATCH],
                        [_HEADER_KEY, "FLAGS", "RFC822.SIZE", "BODYSTRUCTURE"],
                    )
                )
        out: list[MessageSummary] = []
        for uid in uids:
            item = data.get(uid)
            if item is None:
                continue
            headers = _key(item, b"BODY[HEADER") or b""
            structure = item.get(b"BODYSTRUCTURE")
            out.append(
                MessageSummary.from_headers(
                    headers if isinstance(headers, bytes) else bytes(headers),
                    uid=uid,
                    folder=folder,
                    flags=item.get(b"FLAGS", ()),
                    size=item.get(b"RFC822.SIZE"),
                    has_attachments=_walk_has_attachment(structure) if structure else None,
                )
            )
        return out

    def _fetch_full(self, folder: str, uid: int, *, readonly: bool) -> tuple[bytes, list[str]]:
        self._select(folder, readonly=readonly)
        with _errors("message size check"):
            size = (self.client.fetch([uid], ["RFC822.SIZE"]).get(uid) or {}).get(b"RFC822.SIZE")
        if not isinstance(size, int):  # no such uid, or a server that hides the size
            raise MailboxError(f"No message with uid {uid} in '{folder}'.")
        if size > MAX_MESSAGE_BYTES:
            raise MailboxError(
                f"Message {uid} is too large to read ({size // (1024 * 1024)} MB; "
                f"the limit is {MAX_MESSAGE_BYTES // (1024 * 1024)} MB)."
            )
        with _errors("message fetch"):
            data = self.client.fetch([uid], ["BODY.PEEK[]", "FLAGS"])
        item = data.get(uid)
        raw = _key(item, b"BODY[]") if item else None
        if raw is None:
            raise MailboxError(f"No message with uid {uid} in '{folder}'.")
        return bytes(raw), normalize_flags(item.get(b"FLAGS", ()))

    def fetch(self, folder: str, uid: int) -> ParsedMessage:
        """Full message via BODY.PEEK[] on a read-only selection; never sets \\Seen."""
        raw, flags = self._fetch_full(folder, uid, readonly=True)
        return parse_message(raw, uid, folder, flags)

    def fetch_raw(self, folder: str, uid: int) -> bytes:
        """Raw RFC 822 bytes via BODY.PEEK[] (for drafts, attachments)."""
        return self._fetch_full(folder, uid, readonly=True)[0]

    # ------------------------------------------------------------------ changes

    def set_flags(
        self,
        folder: str,
        uids: list[int],
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        if not uids:
            return
        self._select(folder, readonly=False)
        with _errors("flag update"):
            for flag, want in ((_SEEN, seen), (_FLAGGED, flagged)):
                if want is True:
                    self.client.add_flags(uids, [flag])
                elif want is False:
                    self.client.remove_flags(uids, [flag])

    def move(self, folder: str, uids: list[int], destination: str) -> None:
        """UID MOVE, else UID COPY + \\Deleted + UID EXPUNGE <uids> (UIDPLUS), else refuse.

        Never a plain EXPUNGE.
        """
        if not uids:
            return
        if destination == folder:
            raise MailboxError("Source and destination folders are the same.")
        if not self.folder_exists(destination):
            raise MailboxError(f"Destination folder '{destination}' does not exist.")
        use_move = self._has("MOVE")
        if not use_move and not self._has("UIDPLUS"):
            raise MailboxError(
                "This server supports neither MOVE nor UIDPLUS; refusing to move messages "
                "because that could delete other messages.",
                kind="unsupported_move",
            )
        self._select(folder, readonly=False)
        with _errors("move"):
            if use_move:
                self.client.move(uids, destination)
            else:
                self._copy_and_expunge(uids, destination)

    def _copy_and_expunge(self, uids: list[int], destination: str) -> None:
        """UID COPY + \\Deleted + UID EXPUNGE.  If the expunge fails the copies exist, so undo
        the \\Deleted flags (best effort) and say so, rather than leave hidden state behind."""
        self.client.copy(uids, destination)
        self.client.add_flags(uids, [_DELETED], silent=True)
        try:
            self.client.uid_expunge(uids)  # only these UIDs, never a plain EXPUNGE
        except Exception as exc:  # noqa: BLE001 - whatever failed, the messages were copied
            try:
                self.client.remove_flags(uids, [_DELETED], silent=True)
            except Exception:  # noqa: BLE001
                log.warning("could not clear \\Deleted after a failed expunge")
            log.warning("IMAP expunge after copy failed: %s", type(exc).__name__)
            raise MailboxError(
                f"The message(s) were copied to '{destination}' but could not be removed from "
                "the source folder (possible duplicate); nothing was deleted."
            ) from None

    def append(self, folder: str, raw: bytes, flags: tuple[str, ...] = ()) -> int | None:
        """APPEND; returns the new UID when the server reports APPENDUID."""
        with _errors(f"saving message to '{folder}'"):
            resp = self.client.append(folder, raw, flags=_flag_bytes(flags))
        match = _APPENDUID.search(resp if isinstance(resp, bytes) else str(resp).encode())
        return int(match.group(1)) if match else None
