"""smtp.send: TLS ordering, STARTTLS refusal, Bcc handling, secret-free errors."""

from __future__ import annotations

import smtplib
import ssl

import pytest

from posta_mcp import compose, smtp
from posta_mcp.config import Config

PASSWORD = "fake-pw-Qw8!smtp-unit"


def make_config(security: str = "ssl", **extra) -> Config:
    return Config.model_validate(
        {
            "email": "me@example.org",
            "username": "me-login",
            "imap": {"host": "imap.example.org", "port": 993},
            "smtp": {"host": "smtp.example.org", "port": 587, "security": security},
            **extra,
        }
    )


def make_msg():
    return compose.build_message(
        "me@example.org",
        ["Ali <ali@example.org>"],
        "Toplantı ğüşıöç",
        "Merhaba, yarın görüşelim: İıĞğ\n",
        cc=["veli@example.org"],
        bcc=["gizli@example.org"],
    )


class FakeSMTP:
    """Records every call in order, in a class-level journal."""

    journal: list[tuple] = []
    offers_starttls = True
    fail_login: Exception | None = None
    fail_starttls: Exception | None = None
    fail_sendmail: Exception | None = None
    refused: dict = {}

    def __init__(self, host, port, timeout=None, context=None, **kw):
        type(self).journal.append((type(self).__name__, host, port, context))

    def ehlo(self):
        type(self).journal.append(("ehlo",))
        return 250, b"ok"

    def has_extn(self, name):
        type(self).journal.append(("has_extn", name))
        return type(self).offers_starttls and name == "starttls"

    def starttls(self, context=None):
        type(self).journal.append(("starttls", context))
        if type(self).fail_starttls:
            raise type(self).fail_starttls

    def login(self, user, password):
        type(self).journal.append(("login", user, password))
        if type(self).fail_login:
            raise type(self).fail_login

    def sendmail(self, sender, recipients, data):
        type(self).journal.append(("sendmail", sender, list(recipients), data))
        if type(self).fail_sendmail:
            raise type(self).fail_sendmail
        return dict(type(self).refused)

    def send_message(self, *a, **kw):  # pragma: no cover - must never be called
        type(self).journal.append(("send_message",))
        raise AssertionError("send_message would leak Bcc")

    def quit(self):
        type(self).journal.append(("quit",))

    def close(self):
        type(self).journal.append(("close",))


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    for cls in (FakeSMTP, FakeSMTPSSL):
        cls.journal = []
    FakeSMTP.offers_starttls = True
    FakeSMTP.fail_login = FakeSMTP.fail_sendmail = FakeSMTP.fail_starttls = None
    FakeSMTP.refused = {}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setenv("POSTA_PASSWORD", PASSWORD)
    return FakeSMTP


def names(journal):
    return [entry[0] for entry in journal]


def test_ssl_uses_smtp_ssl_with_verifying_context_and_logs_in_with_login():
    smtp.send(make_config("ssl"), make_msg())
    j = FakeSMTPSSL.journal
    assert j[0][0] == "FakeSMTPSSL" and j[0][1:3] == ("smtp.example.org", 587)
    ctx = j[0][3]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert names(j) == ["FakeSMTPSSL", "login", "sendmail", "quit"]
    assert j[1] == ("login", "me-login", PASSWORD)
    assert FakeSMTP.journal == []  # the plain class was never touched


def test_starttls_order_is_ehlo_check_starttls_ehlo_login_send():
    smtp.send(make_config("starttls"), make_msg())
    j = FakeSMTP.journal
    assert names(j) == [
        "FakeSMTP",
        "ehlo",
        "has_extn",
        "starttls",
        "ehlo",
        "login",
        "sendmail",
        "quit",
    ]
    ctx = j[3][1]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_starttls_missing_extension_refuses_before_login_or_send():
    FakeSMTP.offers_starttls = False
    with pytest.raises(smtp.SmtpError, match="STARTTLS"):
        smtp.send(make_config("starttls"), make_msg())
    j = names(FakeSMTP.journal)
    assert "login" not in j and "sendmail" not in j and "starttls" not in j
    assert j[-1] in ("quit", "close")  # connection is closed


@pytest.mark.parametrize(
    "exc",
    [
        ssl.SSLCertVerificationError(1, "bad cert " + PASSWORD),
        smtplib.SMTPResponseException(454, b"TLS not available " + PASSWORD.encode()),
        smtplib.SMTPServerDisconnected("gone " + PASSWORD),
        OSError("reset " + PASSWORD),
    ],
)
def test_starttls_failure_never_logs_in_or_sends_and_closes(exc):
    FakeSMTP.fail_starttls = exc
    with pytest.raises(smtp.SmtpError) as info:
        smtp.send(make_config("starttls"), make_msg())
    j = names(FakeSMTP.journal)
    assert "starttls" in j
    assert "login" not in j and "sendmail" not in j  # the password never left the machine
    assert j[-1] in ("quit", "close")
    assert PASSWORD not in str(info.value)
    assert not any(PASSWORD in str(part) for entry in FakeSMTP.journal for part in entry)


def test_bcc_in_envelope_but_not_in_bytes_and_never_send_message():
    msg = make_msg()
    smtp.send(make_config("ssl"), msg)
    _, sender, recipients, data = next(e for e in FakeSMTPSSL.journal if e[0] == "sendmail")
    assert sender == "me@example.org"
    assert set(recipients) == {"ali@example.org", "veli@example.org", "gizli@example.org"}
    assert b"gizli@example.org" not in data and b"Bcc" not in data
    assert "send_message" not in names(FakeSMTPSSL.journal)
    assert "Bcc" in msg  # the caller's message is not modified


def test_turkish_text_survives_as_utf8():
    smtp.send(make_config("ssl"), make_msg())
    data = next(e for e in FakeSMTPSSL.journal if e[0] == "sendmail")[3]
    from email import message_from_bytes, policy

    parsed = message_from_bytes(data, policy=policy.default)
    assert parsed["Subject"] == "Toplantı ğüşıöç"
    assert "İıĞğ" in parsed.get_body(("plain",)).get_content()


def test_refused_recipients_are_returned_when_others_succeed():
    FakeSMTPSSL.refused = {"gizli@example.org": (550, b"no such user")}
    assert smtp.send(make_config("ssl"), make_msg()) == ["gizli@example.org"]


@pytest.mark.parametrize(
    "where,exc",
    [
        ("login", smtplib.SMTPAuthenticationError(535, b"bad credentials " + PASSWORD.encode())),
        ("sendmail", smtplib.SMTPDataError(554, b"rejected " + PASSWORD.encode())),
        ("sendmail", smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"x")})),
        ("sendmail", smtplib.SMTPServerDisconnected("gone " + PASSWORD)),
        ("sendmail", ssl.SSLCertVerificationError(1, "bad cert " + PASSWORD)),
        ("sendmail", OSError("connection refused " + PASSWORD)),
    ],
)
def test_errors_are_smtp_errors_without_the_password(where, exc):
    setattr(FakeSMTPSSL, "fail_login" if where == "login" else "fail_sendmail", exc)
    with pytest.raises(smtp.SmtpError) as info:
        smtp.send(make_config("ssl"), make_msg())
    assert PASSWORD not in str(info.value)
    assert info.value.__cause__ is None  # `from None`: no chained exception carrying text


def test_missing_password_is_a_clear_secret_free_error(monkeypatch):
    monkeypatch.delenv("POSTA_PASSWORD")
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    with pytest.raises(smtp.SmtpError, match="No password"):
        smtp.send(make_config("ssl"), make_msg())
    assert FakeSMTPSSL.journal == []


def test_no_recipients_is_refused():
    with pytest.raises(smtp.SmtpError):
        smtp.send_raw(make_config("ssl"), "me@example.org", [], b"x")
