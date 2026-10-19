"""Plain-Turkish failure advice: classification, where kinds are attached, what reaches the user."""

from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl

import pytest
from imap_fakes import FakeIMAPClient, factory, raw_message
from imapclient import exceptions as imap_exc
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from posta_mcp import hints
from posta_mcp.cli import try_imap_login
from posta_mcp.config import Config, ConfigError, load_config
from posta_mcp.credentials import CredentialsError
from posta_mcp.imap import Mailbox, MailboxError
from posta_mcp.logs import register_secret
from posta_mcp.server import build_server, build_setup_server
from posta_mcp.smtp import SmtpError
from posta_mcp.tools._common import tool_errors

PASSWORD = "fake-pw-Hn7!hints-unit"

CONFIG = Config.model_validate(
    {
        "email": "me@example.org",
        "imap": {"host": "imap.example.org", "port": 993},
        "smtp": {"host": "smtp.example.org", "port": 465},
    }
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _password(monkeypatch):
    monkeypatch.setenv("POSTA_PASSWORD", PASSWORD)
    FakeIMAPClient.instances.clear()


def _cert_error() -> ssl.SSLCertVerificationError:
    err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    err.verify_message = "self-signed certificate"
    return err


@pytest.mark.parametrize(
    "exc,kind",
    [
        (imap_exc.LoginError("[AUTHENTICATIONFAILED] nope"), "auth"),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "auth"),
        (smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"x")}), "recipient"),
        (_cert_error(), "tls"),
        (socket.gaierror(11001, "getaddrinfo failed"), "dns"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError(10061, "refused"), "unreachable"),
        (ConnectionResetError(), "unreachable"),
        (imaplib.IMAP4.abort("socket error: EOF"), "unreachable"),
        (OSError(10051, "network is unreachable"), "unreachable"),
        (CredentialsError("No password"), "no_password"),
        (CredentialsError("Keychain unavailable", kind="keychain"), "keychain"),
        (MailboxError("x", kind="unsupported_move"), "unsupported_move"),
        (SmtpError("x", kind="insecure"), "insecure"),
        (smtplib.SMTPDataError(554, b"rejected"), None),
        (ssl.SSLError(1, "wrong version number"), None),
        (MailboxError("No message with uid 9"), None),
        (ValueError("bad"), None),
    ],
)
def test_classify(exc, kind):
    assert hints.classify(exc) == kind
    assert (hints.explain(exc) is None) == (kind is None)


def test_every_kind_has_turkish_advice_without_placeholders():
    for kind, text in hints.HINTS.items():
        assert text.strip() and "{" not in text, kind
    assert "mail.bogazici.edu.tr/password" in hints.HINTS["auth"]
    assert "Ayarlar > Uzantılar > Boğaziçi Postanesi" in hints.HINTS["auth"]
    for kind in ("auth", "no_password", "keychain"):
        assert "sohbete yazma" in hints.HINTS[kind]
    assert (
        "posta-mcp parola" in hints.HINTS["auth"] and "posta-mcp parola" in hints.HINTS["keychain"]
    )
    assert "posta-mcp setup" in hints.HINTS["no_password"]
    assert "BU-VPN" in hints.HINTS["unreachable"] and "BU-VPN" in hints.HINTS["timeout"]
    assert "internet" in hints.HINTS["dns"]
    assert "kapatmaya çalışma" in hints.HINTS["tls"]
    assert "sunucunun bir sınırlaması" in hints.HINTS["unsupported_move"]


def test_with_hint_keeps_technical_detail_and_redacts():
    register_secret(PASSWORD)
    text = hints.with_hint(imap_exc.LoginError("x"), f"IMAP login failed ({PASSWORD})")
    first, second = text.split("\n")
    assert first.startswith("Giriş başarısız")
    assert second.startswith("(Teknik ayrıntı: IMAP login failed")
    assert PASSWORD not in text
    assert hints.with_hint(ValueError("x"), "plain detail") == "plain detail"
    fallback = hints.with_hint(ValueError("x"), "d", fallback="Olmadı.")
    assert fallback == "Olmadı.\n(Teknik ayrıntı: d)"


# ---------------------------------------------------------------- kinds attached at the source


def _mailbox(**fake) -> Mailbox:
    config = load_config(
        env={
            "POSTA_PRESET": "generic",
            "POSTA_EMAIL": "me@example.test",
            "POSTA_IMAP_HOST": "imap.example.test",
            "POSTA_SMTP_HOST": "smtp.example.test",
        }
    )
    return Mailbox(config, client_factory=factory(**fake))


def test_imap_login_rejection_is_auth():
    with pytest.raises(MailboxError) as info:
        _mailbox(login_error=f"[AUTHENTICATIONFAILED] {PASSWORD}").__enter__()
    assert info.value.kind == "auth" and PASSWORD not in str(info.value)


def test_imap_certificate_failure_is_tls():
    cfg = load_config(
        env={
            "POSTA_PRESET": "generic",
            "POSTA_EMAIL": "me@example.test",
            "POSTA_IMAP_HOST": "imap.example.test",
            "POSTA_SMTP_HOST": "smtp.example.test",
            "POSTA_IMAP_SECURITY": "starttls",
            "POSTA_IMAP_PORT": "143",
        }
    )
    mb = Mailbox(cfg, client_factory=factory(caps=(b"STARTTLS",), starttls_error=_cert_error()))
    with pytest.raises(MailboxError, match="certificate verification failed") as info:
        mb.__enter__()
    assert info.value.kind == "tls"


@pytest.mark.parametrize(
    "error,kind",
    [
        (socket.gaierror(11001, "getaddrinfo failed"), "dns"),
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError(), "unreachable"),
    ],
)
def test_imap_connection_failures_are_classified(error, kind):
    def broken(*_a, **_kw):
        raise error

    with pytest.raises(MailboxError) as info:
        Mailbox(CONFIG, client_factory=broken).__enter__()
    assert info.value.kind == kind


def test_missing_move_support_is_unsupported_move():
    folders = {"INBOX": {1: raw_message("one")}, "Trash": {}}
    with _mailbox(caps=(b"IMAP4REV1",), folders=folders) as mb:
        with pytest.raises(MailboxError, match="neither MOVE nor UIDPLUS") as info:
            mb.move("INBOX", [1], "Trash")
    assert info.value.kind == "unsupported_move"


def test_missing_password_reaches_imap_as_no_password(monkeypatch):
    monkeypatch.delenv("POSTA_PASSWORD")
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    with pytest.raises(CredentialsError) as info:
        _mailbox().__enter__()
    assert hints.classify(info.value) == "no_password"


# ---------------------------------------------------------------- what the client sees


def test_tool_error_starts_with_the_hint_and_keeps_the_detail():
    register_secret(PASSWORD)
    with pytest.raises(ToolError) as info, tool_errors("Listing folders"):
        raise MailboxError(
            f"IMAP login failed: the server rejected the credentials ({PASSWORD})", kind="auth"
        )
    text = str(info.value)
    assert text.startswith(hints.HINTS["auth"])
    assert "Teknik ayrıntı: Listing folders failed: IMAP login failed" in text
    assert PASSWORD not in text


def test_tool_error_without_a_known_kind_is_unchanged():
    with pytest.raises(ToolError) as info, tool_errors("Reading message"):
        raise MailboxError("No message with uid 9 in 'INBOX'.")
    assert str(info.value) == "Reading message failed: No message with uid 9 in 'INBOX'."


@pytest.mark.anyio
async def test_list_folders_error_through_the_server_carries_the_hint():
    def mailbox():
        raise MailboxError("IMAP login failed: the server rejected the credentials.", kind="auth")

    async with Client(build_server(CONFIG, mailbox=mailbox)) as c:
        result = await c.call_tool("list_folders", {})
    text = result.content[0].text
    assert result.is_error
    assert "Giriş başarısız" in text and "mail.bogazici.edu.tr/password" in text
    assert "login failed" in text  # the English detail stays for debugging


# ---------------------------------------------------------------- setup check in the CLI


class _FakeIMAPClient:
    login_error: Exception | None = None
    connect_error: Exception | None = None

    def __init__(self, *a, **kw):
        if type(self).connect_error:
            raise type(self).connect_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def starttls(self, **kw):
        pass

    def login(self, user, password):
        if type(self).login_error:
            raise type(self).login_error


@pytest.fixture
def fake_imapclient(monkeypatch):
    _FakeIMAPClient.login_error = _FakeIMAPClient.connect_error = None
    monkeypatch.setattr("imapclient.IMAPClient", _FakeIMAPClient)
    return _FakeIMAPClient


def test_try_imap_login_success_is_exactly_ok(fake_imapclient):
    assert try_imap_login(CONFIG, PASSWORD) == "OK"


def test_try_imap_login_failure_is_turkish_and_secret_free(fake_imapclient):
    fake_imapclient.login_error = imap_exc.LoginError(f"b'[AUTHENTICATIONFAILED] {PASSWORD}'")
    status = try_imap_login(CONFIG, PASSWORD)
    assert status != "OK" and status.startswith("Giriş başarısız")
    assert "LoginError" in status and PASSWORD not in status


@pytest.mark.parametrize(
    "error,needle",
    [
        (socket.gaierror(11001, "getaddrinfo failed"), "sunucusu bulunamadı"),
        (TimeoutError("timed out"), "BU-VPN"),
        (_cert_error(), "sertifikası doğrulanamadı"),
        (RuntimeError(f"weird {PASSWORD}"), "Giriş denemesi başarısız"),
    ],
)
def test_try_imap_login_connection_failures(fake_imapclient, error, needle):
    fake_imapclient.connect_error = error
    status = try_imap_login(CONFIG, PASSWORD)
    assert needle in status and "Teknik ayrıntı" in status and PASSWORD not in status


# ---------------------------------------------------------------- setup-required mode


def test_setup_problem_texts():
    missing = hints.setup_problem(missing=["email", "imap.host"])
    assert "e-posta adresi" in missing and "IMAP sunucu adresi" in missing
    bad = hints.setup_problem(error=ConfigError("Unknown preset 'x'. Available: bogazici."))
    assert "Ayarlarda bir sorun var" in bad and "Unknown preset" in bad
    assert "uvx posta-mcp setup" in hints.SETUP_STEPS
    assert "Ayarlar > Uzantılar" in hints.SETUP_STEPS and "sohbete yazma" in hints.SETUP_STEPS


@pytest.mark.anyio
async def test_setup_server_offers_only_server_info_and_baslangic():
    problem = hints.setup_problem(missing=["email"])
    async with Client(build_setup_server(problem)) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        info = await c.call_tool("server_info", {})
        intro = await c.get_prompt("baslangic", {})
        missing = await c.call_tool("list_folders", {})
    assert tools == {"server_info"} and prompts == {"baslangic"}
    data = info.structured_content
    assert data["setup_required"] is True and data["problem"] == problem
    assert "uvx posta-mcp setup" in data["what_to_do"]
    assert "Ayarlar > Uzantılar > Boğaziçi Postanesi" in data["what_to_do"]
    assert "setup_required" in intro.messages[0].content.text
    assert missing.is_error  # no mailbox tools at all in this mode
