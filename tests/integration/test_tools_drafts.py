"""Draft tools through the real MCP server against GreenMail."""

from __future__ import annotations

from email import message_from_bytes, policy

from mcp_call import eml

SEEN = b"\\Seen"
DRAFT = b"\\Draft"


def drafts_of(imap_client, user, folder="Drafts"):
    """[(uid, flags, parsed message)] for every message in `folder`."""
    c = imap_client(user)
    if not c.folder_exists(folder):
        return []
    c.select_folder(folder, readonly=True)
    data = c.fetch(c.search("ALL"), [b"BODY.PEEK[]", b"FLAGS"])
    return [
        (uid, set(d[b"FLAGS"]), message_from_bytes(d[b"BODY[]"], policy=policy.default))
        for uid, d in sorted(data.items())
    ]


def test_create_draft_lands_in_drafts_with_flags(tool, greenmail, imap_client):
    assert not imap_client(greenmail.student).folder_exists("Drafts")
    out = tool(
        "taslak_olustur",
        to=["Hoca Örnek <hoca@bogazici.test>"],
        subject="Ödev teslimi hakkında",
        body="Sayın hocam,\n\nÖdevi yarın teslim edebilir miyim? Teşekkürler.\n",
        cc=["asistan@bogazici.test"],
        bcc=["gizli@example.test"],
    )
    assert not out.is_error, out.text
    assert "NOT been sent" in out.text
    [(uid, flags, msg)] = drafts_of(imap_client, greenmail.student)
    assert {DRAFT, SEEN} <= flags and f"uid {uid}" in out.text
    assert msg["Subject"] == "Ödev teslimi hakkında"
    assert msg["From"].addresses[0].addr_spec == greenmail.student
    assert msg["To"].addresses[0].display_name == "Hoca Örnek"
    assert "Cc" in msg and msg["Bcc"].addresses[0].addr_spec == "gizli@example.test"
    assert "Ödevi yarın teslim edebilir miyim?" in msg.get_body(("plain",)).get_content()
    assert msg["Message-ID"] and msg["Date"]
    # nothing was delivered anywhere
    assert drafts_of(imap_client, greenmail.student, "INBOX") == []
    assert drafts_of(imap_client, greenmail.faculty, "INBOX") == []


def test_create_draft_rejects_bad_input(tool, greenmail, imap_client):
    for args in (
        {"to": ["not-an-address"], "subject": "x", "body": "y"},
        {"to": ["a@example.test"], "subject": "x\r\nBcc: evil@example.test", "body": "y"},
        {"to": [], "subject": "x", "body": "y"},
    ):
        out = tool("taslak_olustur", **args)
        assert out.is_error, args
    assert drafts_of(imap_client, greenmail.student) == []


def test_create_reply_draft_threads_and_leaves_original_unread(
    tool, seed_message, greenmail, imap_client
):
    uid = seed_message(greenmail.student, eml("from_faculty.eml"))
    out = tool(
        "yanit_taslagi_olustur",
        folder="INBOX",
        uid=uid,
        body="Teşekkürler hocam, not edildi.",
    )
    assert not out.is_error, out.text
    assert "NOT been sent" in out.text and "untrusted-email" in out.text
    [(_uid, flags, msg)] = drafts_of(imap_client, greenmail.student)
    assert {DRAFT, SEEN} <= flags
    assert msg["In-Reply-To"] == "<fx-17@fixtures.test>"
    assert str(msg["References"]).split() == [
        "<fx-00@fixtures.test>",
        "<fx-16@fixtures.test>",
        "<fx-17@fixtures.test>",
    ]
    assert msg["Subject"] == "Re: Vize sonuçları"
    assert [a.addr_spec for a in msg["To"].addresses] == ["bolum.test@bogazici.edu.tr"]  # Reply-To
    assert msg["Cc"] is None
    body = msg.get_body(("plain",)).get_content()
    assert body.startswith("Teşekkürler hocam, not edildi.")
    assert "> Vize sonuçları yarın açıklanacaktır." in body
    # the original is still unread
    inbox = drafts_of(imap_client, greenmail.student, "INBOX")
    assert SEEN not in inbox[0][1]


def test_create_reply_draft_reply_all(tool, seed_message, greenmail, imap_client):
    uid = seed_message(greenmail.student, eml("from_student.eml"))
    out = tool("yanit_taslagi_olustur", folder="INBOX", uid=uid, body="Tamam.", reply_all=True)
    assert not out.is_error, out.text
    [(_u, _f, msg)] = drafts_of(imap_client, greenmail.student)
    assert "ogrenci.test@std.bogazici.edu.tr" in str(msg["To"])
    all_rcpt = str(msg["To"]) + str(msg["Cc"])
    assert "deneme.hoca@bogazici.edu.tr" in all_rcpt and "asistan.test@bogazici.edu.tr" in all_rcpt


def test_create_reply_draft_missing_message(tool, greenmail, imap_client):
    out = tool("yanit_taslagi_olustur", folder="INBOX", uid=424242, body="x")
    assert out.is_error and "No message with uid" in out.text
    assert drafts_of(imap_client, greenmail.student) == []
