"""Unit tests for posta_mcp.imap with a recording fake IMAPClient (no network)."""

from __future__ import annotations

import logging
import ssl
from datetime import date

import pytest
from imap_fakes import FakeIMAPClient, factory, raw_message
from imapclient import exceptions as imap_exc

from posta_mcp.config import MAX_MESSAGE_BYTES, load_config
from posta_mcp.imap import Mailbox, MailboxError, SearchCriteria
from posta_mcp.logs import register_secret

PASSWORD = "s3cret-pw-for-tests"


def make_config(**env: str):
    base = {
        "POSTA_PRESET": "generic",
        "POSTA_EMAIL": "me@example.test",
        "POSTA_IMAP_HOST": "imap.example.test",
        "POSTA_SMTP_HOST": "smtp.example.test",
    }
    return load_config(env={**base, **env})


@pytest.fixture(autouse=True)
def _password(monkeypatch):
    monkeypatch.setenv("POSTA_PASSWORD", PASSWORD)
    FakeIMAPClient.instances.clear()


def open_mailbox(config=None, **fake):
    return Mailbox(config or make_config(), client_factory=factory(**fake))


def test_ssl_mode_uses_verifying_context_and_logs_in():
    with open_mailbox() as mb:
        client = FakeIMAPClient.instances[-1]
        assert client.ssl is True and client.port == 993
        assert client.context.verify_mode == ssl.CERT_REQUIRED and client.context.check_hostname
        assert mb.client is client
    assert client.names() == ["login", "logout"]
    assert client.calls[0][1] == ("me@example.test",)


def test_starttls_happens_before_login():
    cfg = make_config(POSTA_IMAP_SECURITY="starttls", POSTA_IMAP_PORT="143")
    with open_mailbox(cfg, caps=(b"IMAP4REV1", b"STARTTLS")):
        client = FakeIMAPClient.instances[-1]
    assert client.ssl is False
    assert client.names()[:2] == ["starttls", "login"]
    assert client.context.verify_mode == ssl.CERT_REQUIRED and client.context.check_hostname


def test_starttls_not_offered_refuses_login():
    cfg = make_config(POSTA_IMAP_SECURITY="starttls", POSTA_IMAP_PORT="143")
    with pytest.raises(MailboxError, match="STARTTLS"):
        open_mailbox(cfg, caps=(b"IMAP4REV1",)).__enter__()
    client = FakeIMAPClient.instances[-1]
    assert "login" not in client.names() and "shutdown" in client.names()


def test_starttls_failure_refuses_login():
    cfg = make_config(POSTA_IMAP_SECURITY="starttls", POSTA_IMAP_PORT="143")
    err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    err.verify_message = "self-signed certificate"
    with pytest.raises(MailboxError, match="certificate verification failed"):
        open_mailbox(cfg, caps=(b"STARTTLS",), starttls_error=err).__enter__()
    assert "login" not in FakeIMAPClient.instances[-1].names()


def test_login_error_is_secret_free():
    register_secret(PASSWORD)
    with pytest.raises(MailboxError) as info:
        open_mailbox(login_error=f"[AUTHENTICATIONFAILED] bad password {PASSWORD}").__enter__()
    assert PASSWORD not in str(info.value)
    assert "me@example.test" in str(info.value)


def test_connection_error_is_wrapped():
    def boom(*a, **k):
        raise ConnectionRefusedError(f"refused while using {PASSWORD}")

    register_secret(PASSWORD)
    with pytest.raises(MailboxError) as info:
        Mailbox(make_config(), client_factory=boom).__enter__()
    assert PASSWORD not in str(info.value) and "imap.example.test:993" in str(info.value)


def test_generic_command_errors_are_redacted():
    register_secret(PASSWORD)
    with open_mailbox() as mb:
        client = FakeIMAPClient.instances[-1]

        def bad_select(*a, **k):
            raise imap_exc.IMAPClientError(f"NO failure {PASSWORD}")

        client.select_folder = bad_select
        with pytest.raises(MailboxError) as info:
            mb.fetch("INBOX", 1)
    assert PASSWORD not in str(info.value)


def test_bodies_fetched_only_with_peek_and_seen_untouched():
    folders = {"INBOX": {7: raw_message("Merhaba")}}
    with open_mailbox(folders=folders) as mb:
        client = FakeIMAPClient.instances[-1]
        msg = mb.fetch("INBOX", 7)
        raw = mb.fetch_raw("INBOX", 7)
        hits = mb.search("INBOX", SearchCriteria(subject="Merhaba"))
    assert msg.subject == "Merhaba" and b"Merhaba" in raw
    assert [h.uid for h in hits] == [7]
    assert "add_flags" not in client.names()
    selects = [args for name, args in client.calls if name == "select_folder"]
    assert selects and all(readonly for _folder, readonly in selects)
    fetch_calls = [args for name, args in client.calls if name == "fetch"]
    fetched = [item.upper() for _uids, items in fetch_calls for item in items]
    assert fetched
    allowed = ("BODY.PEEK[", "FLAGS", "RFC822.SIZE", "BODYSTRUCTURE")
    assert all(item.startswith(allowed) for item in fetched)
    assert not any(item in ("RFC822", "BODY[]") for item in fetched)


def test_fetch_selects_read_only_and_never_flags():
    folders = {"INBOX": {7: raw_message()}}
    with open_mailbox(folders=folders) as mb:
        client = FakeIMAPClient.instances[-1]
        msg = mb.fetch("INBOX", 7)
    assert "\\Seen" not in msg.flags
    names = client.names()
    assert "add_flags" not in names
    assert client.calls[names.index("select_folder")][1] == ("INBOX", True)


def test_fetch_of_a_missing_uid_is_refused_before_the_body_fetch():
    with open_mailbox(folders={"INBOX": {7: raw_message()}}) as mb:
        client = FakeIMAPClient.instances[-1]
        with pytest.raises(MailboxError, match="No message with uid 8"):
            mb.fetch("INBOX", 8)
    fetch_calls = [args for name, args in client.calls if name == "fetch"]
    assert not any("BODY.PEEK[]" in items for _uids, items in fetch_calls)


def test_oversized_message_is_refused_before_its_body_is_fetched():
    big = b"Subject: big\r\n\r\n" + b"x" * MAX_MESSAGE_BYTES
    folders = {"INBOX": {5: big, 6: raw_message("small")}}
    with open_mailbox(folders=folders) as mb:
        client = FakeIMAPClient.instances[-1]
        with pytest.raises(MailboxError, match="too large"):
            mb.fetch("INBOX", 5)
        with pytest.raises(MailboxError, match="too large"):
            mb.fetch_raw("INBOX", 5)
        assert mb.fetch("INBOX", 6).subject == "small"  # the cap does not affect normal mail
    fetched = [item.upper() for name, args in client.calls if name == "fetch" for item in args[1]]
    assert fetched.count("BODY.PEEK[]") == 1  # only the small message's body was ever requested


def test_imap_loggers_stay_at_warning_after_import_and_connect(caplog):
    caplog.set_level(logging.DEBUG)  # root at DEBUG, as POSTA_LOG_LEVEL=DEBUG does
    with open_mailbox() as mb:
        mb.list_folders()
        for name in ("imapclient", "imapclient.imaplib", "imaplib"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


def test_missing_message_is_an_error():
    with open_mailbox() as mb, pytest.raises(MailboxError, match="No message with uid 99"):
        mb.fetch("INBOX", 99)


def test_search_builds_criteria_newest_first_and_caps_limit():
    folders = {"INBOX": {i: raw_message(f"s{i}") for i in range(1, 6)}}
    crit = SearchCriteria(
        from_="ali", subject="toplantı", since=date(2026, 1, 1), unseen=True, flagged=False
    )
    with open_mailbox(folders=folders) as mb:
        client = FakeIMAPClient.instances[-1]
        hits = mb.search("INBOX", crit, limit=3)
    assert [h.uid for h in hits] == [5, 4, 3]
    _, (criteria, charset) = next(c for c in client.calls if c[0] == "search")
    assert criteria == (
        "FROM", "ali", "SUBJECT", "toplantı", "SINCE", date(2026, 1, 1), "UNSEEN", "UNFLAGGED"
    )  # fmt: skip
    assert charset == "UTF-8"  # non-ASCII criterion


def test_search_ascii_criteria_use_default_charset_and_all():
    with open_mailbox() as mb:
        client = FakeIMAPClient.instances[-1]
        mb.search("INBOX", SearchCriteria())
    _, (criteria, charset) = next(c for c in client.calls if c[0] == "search")
    assert criteria == ("ALL",) and charset is None


def test_special_folder_prefers_special_use_then_config_names():
    folders = {"INBOX": {}, "Taslaklar": {}, "Trash": {}}
    with open_mailbox(folders=folders, special={"Taslaklar": b"\\Drafts"}) as mb:
        assert mb.special_folder("drafts") == "Taslaklar"
        assert mb.special_folder("trash") == "Trash"
        with pytest.raises(MailboxError, match="archive"):
            mb.special_folder("archive")


def test_ensure_special_folder_creates_only_when_missing():
    with open_mailbox(folders={"INBOX": {}, "Trash": {}}) as mb:
        client = FakeIMAPClient.instances[-1]
        assert mb.ensure_special_folder("trash") == "Trash"
        assert "create_folder" not in client.names()
        assert mb.ensure_special_folder("drafts") == "Drafts"
        assert ("create_folder", ("Drafts",)) in client.calls


def test_append_returns_appenduid():
    with open_mailbox() as mb:
        uid = mb.append("INBOX", b"Subject: x\r\n\r\nhi", ("\\Draft", "\\Seen"))
    assert uid == 1
