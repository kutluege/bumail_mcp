"""Read tools through the real MCP server against GreenMail (Turkish fixtures)."""

from __future__ import annotations

import re
from datetime import date, timedelta

from mcp_call import eml

pytest_plugins = ["mcp_call"]  # provides the `tool` fixture

SEEN = b"\\Seen"


def flags_of(imap_client, user, uid, folder="INBOX"):
    c = imap_client(user)
    c.select_folder(folder, readonly=True)
    return c.get_flags([uid])[uid]


def test_list_folders_wrapped(tool, seed_message, greenmail):
    seed_message(greenmail.student, eml("from_faculty.eml"), folder="Okul")
    out = tool("list_folders")
    assert not out.is_error
    assert "INBOX" in out.text and "Okul" in out.text
    assert "<untrusted-email" in out.text


def test_search_and_get_turkish_message(tool, seed_message, greenmail, imap_client):
    uid = seed_message(greenmail.student, eml("from_faculty.eml"))
    seed_message(greenmail.student, eml("windows1254_body.eml"))

    found = tool("search_messages", subject="sonuçları")  # non-ASCII criterion (UTF-8 charset)
    assert not found.is_error, found.text
    assert f"uid={uid}" in found.text and "Vize sonuçları" in found.text
    assert "Fiyat" not in found.text

    msg = tool("get_message", folder="INBOX", uid=uid)
    assert not msg.is_error, msg.text
    assert "İtirazlar için bölüm sekreterliğine başvurun." in msg.text
    assert "Prof. Dr. Deneme Hoca" in msg.text
    assert "Reply-To:" in msg.text and "Bölüm Sekreteri" in msg.text
    assert "<untrusted-email" in msg.text and "NOTICE" in msg.text


def test_reading_leaves_seen_unset_non_negotiable_6(tool, seed_message, greenmail, imap_client):
    uid = seed_message(greenmail.student, eml("from_faculty.eml"))
    tool("search_messages")
    tool("get_message", folder="INBOX", uid=uid)
    tool("get_message", folder="INBOX", uid=uid, mark_seen=False)
    assert SEEN not in flags_of(imap_client, greenmail.student, uid)


def test_get_message_mark_seen_sets_seen(tool, seed_message, greenmail, imap_client):
    uid = seed_message(greenmail.student, eml("from_faculty.eml"))
    out = tool("get_message", folder="INBOX", uid=uid, mark_seen=True)
    assert not out.is_error
    assert SEEN in flags_of(imap_client, greenmail.student, uid)


def test_search_filters_order_and_limit(tool, seed_message, greenmail):
    u1 = seed_message(greenmail.student, eml("from_faculty.eml"), flags=(SEEN,))
    u2 = seed_message(greenmail.student, eml("from_student.eml"), flags=(b"\\Flagged",))
    u3 = seed_message(greenmail.student, eml("mailing_list.eml"))

    everything = tool("search_messages").text
    order = [int(n) for n in re.findall(r"uid=(\d+)", everything)]
    assert order == [u3, u2, u1]  # newest first

    assert re.findall(r"uid=(\d+)", tool("search_messages", limit=2).text) == [str(u3), str(u2)]
    assert "(Showing the newest 2" in tool("search_messages", limit=2).text
    assert f"uid={u1}" not in tool("search_messages", unseen=True).text
    assert re.findall(r"uid=(\d+)", tool("search_messages", flagged=True).text) == [str(u2)]
    # GreenMail matches FROM against the whole address only (real servers do substring)
    by_sender = tool("search_messages", from_="duyuru.test@bogazici.edu.tr").text
    assert re.findall(r"uid=(\d+)", by_sender) == [str(u3)]
    assert "list: " in by_sender
    assert "[students]" in tool("search_messages", text="Yemekhane").text
    # SINCE/BEFORE use INTERNALDATE (= APPEND time), not the Date header
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert tool("search_messages", before=yesterday).text.count("uid=") == 0
    assert tool("search_messages", since=yesterday, before=tomorrow).text.count("uid=") == 3
    assert tool("search_messages", since=tomorrow).text.count("uid=") == 0
    bad = tool("search_messages", since="03/02/2026")
    assert bad.is_error and "ISO date" in bad.text


def test_attachments_listed_and_text_extracted(tool, seed_message, greenmail):
    uid = seed_message(greenmail.student, eml("attachments_tr.eml"))
    summary = tool("search_messages")
    assert "attachments" in summary.text
    msg = tool("get_message", folder="INBOX", uid=uid)
    assert "Ders Programı Şubat.txt" in msg.text and "Transkript Çıktısı.pdf" in msg.text
    att = tool("get_attachment_text", folder="INBOX", uid=uid, index=0)
    assert not att.is_error, att.text
    assert "Pazartesi: Fizik" in att.text and "Salı: Kimya Çalışması" in att.text
    assert 'kind="attachment"' in att.text
    csv = tool("get_attachment_text", folder="INBOX", uid=uid, index=1)
    assert "İpek;90" in csv.text
    pdf = tool("get_attachment_text", folder="INBOX", uid=uid, index=2)
    assert pdf.is_error


def test_injection_cannot_escape_wrapper(tool, seed_message, greenmail):
    uid = seed_message(greenmail.student, eml("injection_plain.eml"))
    out = tool("get_message", folder="INBOX", uid=uid).text
    nonce = re.search(r'<untrusted-email nonce="([0-9a-f]+)"', out).group(1)
    assert nonce != "deadbeefdeadbeef"
    assert out.count(f'</untrusted-email nonce="{nonce}">') == 1
    assert out.rstrip().endswith(f'</untrusted-email nonce="{nonce}">')
    assert '</untrusted-email nonce="deadbeefdeadbeef">' not in out
    summary = tool("search_messages").text
    assert summary.rstrip().endswith(
        '</untrusted-email nonce="' + re.search(r'nonce="([0-9a-f]+)"', summary).group(1) + '">'
    )


def test_max_chars_truncates(tool, seed_message, greenmail):
    raw = b"From: a@example.test\r\nSubject: uzun\r\n\r\n" + b"x" * 1000 + b"\r\n"
    uid = seed_message(greenmail.student, raw)
    out = tool("get_message", folder="INBOX", uid=uid, max_chars=200)
    assert not out.is_error and "truncated: showing 200 of 1000" in out.text


def test_errors_are_short_and_secret_free(tool, greenmail):
    out = tool("get_message", folder="NoSuchFolder", uid=1)
    assert out.is_error and greenmail.password(greenmail.student) not in out.text
    missing = tool("get_message", folder="INBOX", uid=999)
    assert missing.is_error and "No message with uid 999" in missing.text
    tool.use(greenmail.student, POSTA_PASSWORD="wrong-fake-password")
    bad = tool("search_messages")
    assert bad.is_error and "wrong-fake-password" not in bad.text and "login failed" in bad.text
