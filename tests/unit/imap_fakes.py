"""A recording fake of ``imapclient.IMAPClient`` for unit tests of ``posta_mcp.imap``.

It enforces the safety rules by raising ``AssertionError`` when the code under test breaks them:
plain EXPUNGE, CLOSE, non-PEEK body fetches, login before STARTTLS.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

from imapclient import exceptions as imap_exc

DEFAULT_CAPS = (b"IMAP4REV1", b"MOVE", b"UIDPLUS", b"SORT")


def raw_message(subject: str = "Hello", body: str = "Body text", **headers: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = headers.pop("From", "Ali Veli <ali@example.test>")
    msg["To"] = headers.pop("To", "me@example.test")
    msg["Subject"] = subject
    msg["Date"] = "Mon, 05 Jan 2026 10:00:00 +0000"
    msg["Message-ID"] = headers.pop("Message-ID", "<m1@example.test>")
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAPClient:
    instances: list[FakeIMAPClient] = []

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_on: bool,
        context: Any,
        timeout: float,
        caps: tuple[bytes, ...] = DEFAULT_CAPS,
        folders: dict[str, dict[int, bytes]] | None = None,
        login_error: str | None = None,
        starttls_error: Exception | None = None,
        special: dict[str, bytes] | None = None,
    ) -> None:
        self.special = special or {}
        self.host, self.port, self.ssl, self.context = host, port, ssl_on, context
        self.caps = caps
        self.folders = folders if folders is not None else {"INBOX": {}}
        self.flags: dict[tuple[str, int], set[bytes]] = {}
        self.login_error = login_error
        self.starttls_error = starttls_error
        self.calls: list[tuple[str, tuple]] = []
        self.tls_active = ssl_on
        self.logged_in = False
        self.selected: str | None = None
        self.readonly = False
        FakeIMAPClient.instances.append(self)

    # -- helpers
    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # -- session
    def has_capability(self, cap: str) -> bool:
        return cap.encode().upper() in self.caps

    def starttls(self, context: Any) -> None:
        self._rec("starttls", context)
        if self.starttls_error:
            raise self.starttls_error
        self.tls_active = True
        self.context = context

    def login(self, user: str, password: str) -> None:
        self._rec("login", user)
        assert self.tls_active, "login attempted over an unencrypted connection"
        if self.login_error:
            raise imap_exc.LoginError(self.login_error)
        self.logged_in = True

    def logout(self) -> None:
        self._rec("logout")

    def shutdown(self) -> None:
        self._rec("shutdown")

    # -- folders
    def list_folders(self):
        self._rec("list_folders")
        return [((self.special[n],) if n in self.special else (), b".", n) for n in self.folders]

    def create_folder(self, name: str) -> None:
        self._rec("create_folder", name)
        self.folders[name] = {}

    def select_folder(self, name: str, readonly: bool = False):
        self._rec("select_folder", name, readonly)
        if name not in self.folders:
            raise imap_exc.IMAPClientError("[NONEXISTENT] no such folder")
        self.selected, self.readonly = name, readonly

    def close_folder(self):
        raise AssertionError("CLOSE would silently expunge \\Deleted messages")

    # -- messages
    def search(self, criteria, charset=None):
        self._rec("search", tuple(criteria), charset)
        return list(self.folders[self.selected])

    def fetch(self, uids, items):
        self._rec("fetch", tuple(uids), tuple(items))
        out: dict[int, dict] = {}
        for uid in uids:
            raw = self.folders[self.selected].get(uid)
            if raw is None:
                continue
            row: dict[bytes, Any] = {}
            for item in items:
                up = item.upper()
                if up.startswith("BODY.PEEK[HEADER.FIELDS"):
                    head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                    row[b"BODY[HEADER.FIELDS (FROM TO)]"] = head
                elif up == "BODY.PEEK[]":
                    row[b"BODY[]"] = raw
                elif up == "FLAGS":
                    row[b"FLAGS"] = tuple(self.flags.get((self.selected, uid), ()))
                elif up == "RFC822.SIZE":
                    row[b"RFC822.SIZE"] = len(raw)
                elif up == "BODYSTRUCTURE":
                    row[b"BODYSTRUCTURE"] = (b"text", b"plain", None)
                else:
                    raise AssertionError(f"unexpected FETCH item {item!r} (only PEEK allowed)")
            out[uid] = row
        return out

    def add_flags(self, uids, flags, silent=False):
        self._rec("add_flags", tuple(uids), tuple(flags))
        assert not self.readonly, "flags changed on a read-only selection"
        for u in uids:
            self.flags.setdefault((self.selected, u), set()).update(flags)

    def remove_flags(self, uids, flags, silent=False):
        self._rec("remove_flags", tuple(uids), tuple(flags))
        for u in uids:
            self.flags.setdefault((self.selected, u), set()).difference_update(flags)

    def move(self, uids, dest):
        self._rec("move", tuple(uids), dest)
        for u in uids:
            self.folders.setdefault(dest, {})[u] = self.folders[self.selected].pop(u)

    def copy(self, uids, dest):
        self._rec("copy", tuple(uids), dest)
        for u in uids:
            self.folders.setdefault(dest, {})[u] = self.folders[self.selected][u]

    def uid_expunge(self, uids):
        self._rec("uid_expunge", tuple(uids))
        assert uids, "UID EXPUNGE without uids"
        assert b"UIDPLUS" in self.caps
        for u in uids:
            self.folders[self.selected].pop(u, None)

    def expunge(self, messages=None):
        self._rec("expunge", messages)
        raise AssertionError("plain EXPUNGE must never be issued")

    def append(self, folder, msg, flags=(), msg_time=None):
        self._rec("append", folder, tuple(flags))
        uid = max(self.folders.setdefault(folder, {}), default=0) + 1
        self.folders[folder][uid] = bytes(msg)
        return f"[APPENDUID 1 {uid}] APPEND completed".encode()


def factory(**kwargs: Any):
    """A ``client_factory`` for ``Mailbox`` building FakeIMAPClients with preset behaviour."""

    def make(host, port, *, ssl_on, context, timeout):
        return FakeIMAPClient(host, port, ssl_on=ssl_on, context=context, timeout=timeout, **kwargs)

    return make
