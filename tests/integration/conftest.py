"""Fixtures for tests that talk to a real (local) GreenMail server.

Start the server first:  ``uv run python scripts/greenmail.py certs && ... start``
(see docs/TESTING.md).  Every test under ``tests/integration`` is automatically marked
``greenmail``; it is skipped when GreenMail is unreachable, or FAILS when the environment
variable ``POSTA_REQUIRE_GREENMAIL=1`` (CI sets it).

Public fixtures for other test modules:

* ``greenmail``                  -> :class:`GreenmailInfo` (host, ports, ca_file, users)
* ``imap_client(user)``          -> logged-in ``imapclient.IMAPClient`` over verified IMAPS
* ``seed_message(user, raw, folder="INBOX", flags=())`` -> UID of the appended message
* ``smtp_send(from_user, msg, to=None)`` -> send an ``EmailMessage`` over verified SMTPS
* ``posta_env(user=None, **overrides)``  -> dict of POSTA_* env vars pointing posta-mcp at
  GreenMail (pass it as ``env=`` when spawning the server, or ``monkeypatch.setenv`` it)

Per test (autouse for greenmail tests): all messages and non-INBOX folders of every test user
are removed (this is setup of a throwaway test server, not product code, so plain EXPUNGE is
fine here) and ``POSTA_CA_FILE`` is set in the environment.
"""

from __future__ import annotations

import importlib.util
import os
import re
import socket
import ssl
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import imapclient
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "greenmail.py"
_spec = importlib.util.spec_from_file_location("posta_greenmail_script", _SCRIPT)
assert _spec and _spec.loader
_gm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _gm  # dataclasses need the module registered
_spec.loader.exec_module(_gm)  # single source of truth for ports/users/paths

HOST = "127.0.0.1"  # a SAN of the test cert; "localhost" is slow on Windows (tries ::1 first)


@dataclass(frozen=True)
class GreenmailInfo:
    host: str
    imaps_port: int
    smtps_port: int
    ca_file: Path
    users: dict[str, str] = field(default_factory=dict)  # e-mail (== login) -> fake password

    @property
    def student(self) -> str:
        return "ogrenci@std.bogazici.test"

    @property
    def faculty(self) -> str:
        return "hoca@bogazici.test"

    @property
    def outsider(self) -> str:
        return "diger@example.test"

    def password(self, user: str) -> str:
        return self.users[user]

    def ssl_context(self) -> ssl.SSLContext:
        """Default (fully verifying) context with the test CA added."""
        return ssl.create_default_context(cafile=str(self.ca_file))


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.greenmail)


def _reachable(inst) -> bool:
    for port in (inst.imaps_port, inst.smtps_port):
        try:
            with socket.create_connection((HOST, port), timeout=2):
                pass
        except OSError:
            return False
    return True


@pytest.fixture(scope="session")
def greenmail() -> GreenmailInfo:
    required = os.environ.get("POSTA_REQUIRE_GREENMAIL") == "1"
    inst = _gm.get_instance()  # honours POSTA_GREENMAIL_INSTANCE
    hint = f" --instance {inst.name}" if inst.name else ""
    problem = None
    if not _gm.CA_PEM.exists():
        problem = f"{_gm.CA_PEM} missing (run: uv run python scripts/greenmail.py certs)"
    elif not _reachable(inst):
        problem = f"GreenMail not reachable (run: uv run python scripts/greenmail.py start{hint})"
    if problem:
        if required:
            pytest.fail(f"POSTA_REQUIRE_GREENMAIL=1 but {problem}", pytrace=False)
        pytest.skip(problem)
    return GreenmailInfo(HOST, inst.imaps_port, inst.smtps_port, _gm.CA_PEM, dict(_gm.USERS))


def _connect(gm: GreenmailInfo, user: str) -> imapclient.IMAPClient:
    client = imapclient.IMAPClient(
        gm.host, port=gm.imaps_port, ssl=True, ssl_context=gm.ssl_context(), timeout=30
    )
    client.login(user, gm.password(user))
    return client


def _wipe(gm: GreenmailInfo, user: str) -> None:
    client = _connect(gm, user)
    try:
        folders = [name for _flags, _sep, name in client.list_folders()]
        for name in folders:
            client.select_folder(name)
            uids = client.search("ALL")
            if uids:
                client.add_flags(uids, [b"\\Deleted"])
                client.expunge()  # test-server cleanup only; never do this in product code
        client.select_folder("INBOX")  # GreenMail has no UNSELECT; leave the folder first
        # deepest first so child folders go before parents
        for name in sorted(folders, key=lambda n: (n.count("."), len(n)), reverse=True):
            if name.upper() != "INBOX":
                client.delete_folder(name)
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001, S110
            pass


@pytest.fixture(autouse=True)
def _greenmail_clean(request, monkeypatch):
    """Wipe test mailboxes and point POSTA_CA_FILE at the test CA before each test."""
    if request.node.get_closest_marker("greenmail") is None:
        yield
        return
    gm: GreenmailInfo = request.getfixturevalue("greenmail")
    for user in gm.users:
        _wipe(gm, user)
    monkeypatch.setenv("POSTA_CA_FILE", str(gm.ca_file))
    yield


@pytest.fixture
def imap_client(greenmail) -> Callable[[str], imapclient.IMAPClient]:
    """Factory: ``imap_client(user)`` -> logged-in IMAPClient (verified TLS, test CA)."""
    clients: list[imapclient.IMAPClient] = []

    def make(user: str) -> imapclient.IMAPClient:
        c = _connect(greenmail, user)
        clients.append(c)
        return c

    yield make
    for c in clients:
        try:
            c.logout()
        except Exception:  # noqa: BLE001, S110
            pass


_APPENDUID = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")


@pytest.fixture
def seed_message(greenmail) -> Callable[..., int]:
    """``seed_message(user, raw_bytes, folder="INBOX", flags=())`` -> UID (via IMAP APPEND).

    The folder is created if missing.  ``flags`` is e.g. ``(b"\\Seen",)``.
    """

    def seed(
        user: str, raw: bytes, folder: str = "INBOX", flags: Iterable[bytes | str] = ()
    ) -> int:
        c = _connect(greenmail, user)
        try:
            if not c.folder_exists(folder):
                c.create_folder(folder)
            resp = c.append(folder, raw, flags=list(flags))
            m = _APPENDUID.search(resp if isinstance(resp, bytes) else str(resp).encode())
            if m:
                return int(m.group(1))
            c.select_folder(folder)  # server did not report APPENDUID: newest UID is ours
            return max(c.search("ALL"))
        finally:
            c.logout()

    return seed


@pytest.fixture
def smtp_send(greenmail) -> Callable[..., None]:
    """``smtp_send(from_user, msg, to=None)``: authenticated send over verified SMTPS."""
    import smtplib

    def send(from_user: str, msg: EmailMessage, to: list[str] | None = None) -> None:
        with smtplib.SMTP_SSL(
            greenmail.host, greenmail.smtps_port, context=greenmail.ssl_context(), timeout=30
        ) as s:
            s.login(from_user, greenmail.password(from_user))
            s.send_message(msg, from_addr=from_user, to_addrs=to)

    return send


@pytest.fixture
def posta_env(greenmail) -> Callable[..., dict[str, str]]:
    """``posta_env(user=None, **overrides)`` -> POSTA_* env vars for the posta-mcp process.

    Defaults to the student account.  Overrides replace keys verbatim, e.g.
    ``posta_env(POSTA_ALLOW_SEND="1")``; a value of ``None`` drops the key.
    """

    def env(user: str | None = None, **overrides: str | None) -> dict[str, str]:
        user = user or greenmail.student
        out = {
            "POSTA_PRESET": "generic",
            "POSTA_EMAIL": user,
            "POSTA_USERNAME": user,
            "POSTA_PASSWORD": greenmail.password(user),
            "POSTA_IMAP_HOST": greenmail.host,
            "POSTA_IMAP_PORT": str(greenmail.imaps_port),
            "POSTA_IMAP_SECURITY": "ssl",
            "POSTA_SMTP_HOST": greenmail.host,
            "POSTA_SMTP_PORT": str(greenmail.smtps_port),
            "POSTA_SMTP_SECURITY": "ssl",
            "POSTA_CA_FILE": str(greenmail.ca_file),
        }
        for k, v in overrides.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = v
        return out

    return env
