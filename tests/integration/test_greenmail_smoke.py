"""Smoke tests: the GreenMail test server works the way the product code will need it to."""

from __future__ import annotations

import email
import time
from email import policy
from email.message import EmailMessage

import pytest

TR = "ğüşıöçİ"


def _fetch_raw(client, uid: int) -> tuple[bytes, tuple]:
    data = client.fetch([uid], [b"BODY.PEEK[]", b"FLAGS"])[uid]
    return data[b"BODY[]"], data[b"FLAGS"]


def _wait_for(client, count: int, timeout: float = 10) -> list[int]:
    deadline = time.time() + timeout
    while True:
        client.select_folder("INBOX")
        uids = client.search("ALL")
        if len(uids) >= count or time.time() > deadline:
            return uids
        time.sleep(0.2)


def test_turkish_utf8_roundtrip_peek_leaves_unseen(greenmail, smtp_send, imap_client):
    msg = EmailMessage()
    msg["From"] = greenmail.faculty
    msg["To"] = greenmail.student
    msg["Subject"] = f"Toplantı duyurusu {TR}"
    msg.set_content(f"Merhaba,\n\nYarınki toplantı için: {TR}\n", charset="utf-8")
    smtp_send(greenmail.faculty, msg)

    c = imap_client(greenmail.student)
    uids = _wait_for(c, 1)
    assert len(uids) == 1
    raw, flags = _fetch_raw(c, uids[0])
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed["Subject"] == f"Toplantı duyurusu {TR}"
    assert TR in parsed.get_body(("plain",)).get_content()
    assert b"\\Seen" not in flags

    # a second PEEK fetch, and a fresh connection, still see it unseen
    _raw2, flags2 = _fetch_raw(c, uids[0])
    assert b"\\Seen" not in flags2
    c2 = imap_client(greenmail.student)
    c2.select_folder("INBOX")
    assert b"\\Seen" not in c2.get_flags([uids[0]])[uids[0]]


def test_windows_1254_append_and_fetch(greenmail, seed_message, imap_client):
    subject = (
        "=?windows-1254?B?"
        + __import__("base64").b64encode(f"Duyuru {TR}".encode("cp1254")).decode()
        + "?="
    )
    raw = (
        b"From: hoca@bogazici.test\r\n"
        b"To: ogrenci@std.bogazici.test\r\n"
        b"Subject: " + subject.encode("ascii") + b"\r\n"
        b"Date: Mon, 05 Oct 2026 10:00:00 +0300\r\n"
        b"Message-ID: <cp1254-1@bogazici.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=windows-1254\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + f"Görüşmek üzere: {TR}\r\n".encode("cp1254")
    )
    uid = seed_message(greenmail.student, raw)
    c = imap_client(greenmail.student)
    c.select_folder("INBOX")
    got, flags = _fetch_raw(c, uid)
    assert got == raw
    assert b"\\Seen" not in flags
    parsed = email.message_from_bytes(got, policy=policy.default)
    assert parsed["Subject"] == f"Duyuru {TR}"
    assert TR in parsed.get_content()


def test_seed_message_flags_and_folder(greenmail, seed_message, imap_client):
    raw = b"From: a@example.test\r\nSubject: x\r\n\r\nbody\r\n"
    uid = seed_message(greenmail.student, raw, folder="Archive", flags=(b"\\Seen",))
    c = imap_client(greenmail.student)
    c.select_folder("Archive")
    assert b"\\Seen" in c.get_flags([uid])[uid]


def test_cleanup_between_tests_leaves_mailbox_empty(greenmail, imap_client):
    # runs after the tests above; the autouse cleanup must have removed everything
    c = imap_client(greenmail.student)
    assert [n for _f, _s, n in c.list_folders()] == ["INBOX"]
    c.select_folder("INBOX")
    assert c.search("ALL") == []


def test_posta_env_shape(greenmail, posta_env):
    env = posta_env()
    assert env["POSTA_PRESET"] == "generic"
    assert env["POSTA_IMAP_SECURITY"] == "ssl" and env["POSTA_SMTP_SECURITY"] == "ssl"
    assert env["POSTA_CA_FILE"] == str(greenmail.ca_file)
    assert "POSTA_PASSWORD" not in posta_env(POSTA_PASSWORD=None)


# --- capabilities the product depends on (non-negotiable 5) ---------------------------------


def test_advertised_capabilities(imap_client, greenmail):
    caps = {c.upper() for c in imap_client(greenmail.student).capabilities()}
    assert {b"MOVE", b"UIDPLUS", b"IDLE"} <= caps
    assert b"STARTTLS" not in caps  # GreenMail has no STARTTLS; IMAPS only
    assert b"SPECIAL-USE" not in caps  # so Trash/Archive must come from names/config


def test_uid_move(greenmail, seed_message, imap_client):
    u1 = seed_message(greenmail.student, b"Subject: one\r\n\r\n1\r\n")
    u2 = seed_message(greenmail.student, b"Subject: two\r\n\r\n2\r\n")
    c = imap_client(greenmail.student)
    c.create_folder("Trash")
    c.select_folder("INBOX")
    c.move([u1], "Trash")
    assert c.search("ALL") == [u2]
    c.select_folder("Trash")
    assert len(c.search("ALL")) == 1


def test_uid_copy_then_uid_expunge_only_targets(greenmail, seed_message, imap_client):
    """Fallback path: UID COPY + \\Deleted + UID EXPUNGE <uids>, never plain EXPUNGE."""
    u1 = seed_message(greenmail.student, b"Subject: one\r\n\r\n1\r\n")
    u2 = seed_message(greenmail.student, b"Subject: two\r\n\r\n2\r\n")
    c = imap_client(greenmail.student)
    c.create_folder("Trash")
    c.select_folder("INBOX")
    c.copy([u1], "Trash")
    # u2 is \Deleted too, but not targeted: UID EXPUNGE must leave it alone
    c.add_flags([u1, u2], [b"\\Deleted"])
    c.uid_expunge([u1])
    remaining = c.search("ALL")
    assert remaining == [u2]
    assert b"\\Deleted" in c.get_flags([u2])[u2]
    c.select_folder("Trash")
    assert len(c.search("ALL")) == 1


@pytest.mark.parametrize("name", ["Trash", "Archive"])
def test_no_special_use_attributes(name, greenmail, imap_client):
    c = imap_client(greenmail.student)
    c.create_folder(name)
    flags = {n: f for f, _s, n in c.list_folders()}
    assert not any(
        f.startswith(b"\\") and f.lower() in (b"\\trash", b"\\archive") for f in flags[name]
    )
