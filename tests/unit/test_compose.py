"""Message composition: encoding, threading, header-injection rejection, Bcc handling."""

from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

import pytest

from posta_mcp.compose import (
    build_message,
    build_reply,
    envelope_recipients,
    message_bytes,
    parse_address,
    reply_subject,
    send_bytes,
)
from posta_mcp.mime import parse_message

EML = Path(__file__).resolve().parent.parent / "fixtures" / "eml"
ME = "kullanici.test@std.bogazici.edu.tr"


def reparse(data: bytes):
    return message_from_bytes(data, policy=policy.default)


def original(name: str = "from_faculty.eml"):
    return parse_message((EML / name).read_bytes(), 17, "INBOX")


# ------------------------------------------------------------------ build_message


def test_build_message_utf8_and_rfc2047():
    msg = build_message(
        f"Kullanıcı Şahin <{ME}>",
        ["Öğretmen Çelik <ogretmen.test@bogazici.edu.tr>", "b@example.test"],
        "Ödev teslimi: İstanbul'dan Şubat",
        "Merhaba,\n\nİstanbul'da ğüşıöç ĞÜŞİÖÇ çalışma.\n",
        cc=["cc.test@bogazici.edu.tr"],
    )
    data = msg.as_bytes()
    assert data.isascii() is False or b"=?utf-8?" in data.lower()
    head = data.split(b"\r\n\r\n", 1)[0]
    assert head.isascii() and b"=?utf-8?" in head.lower()  # headers are pure ASCII on the wire
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    back = reparse(data)
    assert back["Subject"] == "Ödev teslimi: İstanbul'dan Şubat"
    assert back["From"].addresses[0].display_name == "Kullanıcı Şahin"
    assert back["From"].addresses[0].addr_spec == ME
    assert back["To"].addresses[0].display_name == "Öğretmen Çelik"
    assert back["Cc"].addresses[0].addr_spec == "cc.test@bogazici.edu.tr"
    assert (
        back.get_content().replace("\r\n", "\n")
        == "Merhaba,\n\nİstanbul'da ğüşıöç ĞÜŞİÖÇ çalışma.\n"
    )
    assert back.get_content_charset() == "utf-8"
    assert back["Date"] and back["Message-ID"].endswith("@std.bogazici.edu.tr>")
    assert back["MIME-Version"] == "1.0"


def test_build_message_roundtrips_through_our_parser():
    msg = build_message(ME, "a@example.test", "Ağır çekim", "Güzel bir gün.")
    parsed = parse_message(msg.as_bytes(), 1, "Drafts")
    assert (parsed.subject, parsed.body, parsed.from_addr) == ("Ağır çekim", "Güzel bir gün.", ME)


def test_idn_domain_goes_out_as_punycode():
    assert b"xn--" in build_message(ME, "a@bücher.example", "s", "b").as_bytes()


def test_to_may_be_a_single_string_and_dedupes_envelope():
    msg = build_message(
        ME, "A@Example.test", "s", "b", cc=["a@example.test"], bcc=["z@example.test"]
    )
    assert envelope_recipients(msg) == ["A@Example.test", "z@example.test"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"subject": "hi\r\nBcc: evil@example.test"},
        {"subject": "hi\nBcc: evil@example.test"},
        {"subject": "hi\rX: y"},
        {"subject": "hi\x00there"},
        {"to": "a@example.test\r\nBcc: evil@example.test"},
        {"to": ["ok@example.test", "b@example.test\nBcc: evil@example.test"]},
        {"to": "Evil\r\nBcc: evil@example.test <a@example.test>"},
        {"to": 'Evil "\n" <a@example.test>'},
        {"to": "a@example.test\x00"},
        {"cc": ["x@example.test\r\nBcc: e@example.test"]},
        {"bcc": ["x@example.test\n"]},
        {"from_addr": "me@example.test\r\nBcc: evil@example.test"},
        {"from_addr": "Me\r\n <me@example.test>"},
        {"in_reply_to": "<a@b.test>\r\nBcc: x@example.test"},
        {"references": ["<a@b.test>", "<c@d.test>\nBcc: x@example.test"]},
        {"body": "hello\x00"},
    ],
)
def test_header_injection_is_rejected(kwargs):
    args = {"from_addr": ME, "to": "a@example.test", "subject": "s", "body": "b"} | kwargs
    with pytest.raises(ValueError):
        build_message(**args)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plain",
        "a@",
        "@b.test",
        "a@b@c.test",
        "a b@c.test",
        "a@b.test, c@d.test",
        "a@@b.test",
        "<a@b.test",
        "a@.test",
        "a@b..test",
        "üser@b.test",
        'a"b@c.test',
        "Name <>",
    ],
)
def test_invalid_addresses_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_address(bad)
    with pytest.raises(ValueError):
        build_message(ME, bad, "s", "b")


def test_requires_a_recipient_and_valid_references():
    with pytest.raises(ValueError):
        build_message(ME, [], "s", "b")
    with pytest.raises(ValueError):
        build_message(ME, "a@example.test", "s", "b", in_reply_to="not-an-id")


def test_in_reply_to_and_references_headers():
    msg = build_message(
        ME,
        "a@example.test",
        "s",
        "b",
        in_reply_to="<b@x.test>",
        references=["<a@x.test>", "<b@x.test>"],
    )
    back = reparse(msg.as_bytes())
    assert back["In-Reply-To"] == "<b@x.test>" and back["References"] == "<a@x.test> <b@x.test>"


# ------------------------------------------------------------------ Bcc


def test_bcc_is_in_draft_bytes_but_not_in_send_bytes():
    msg = build_message(
        ME, "a@example.test", "s", "b", cc=["c@example.test"], bcc=["secret@example.test"]
    )
    assert b"secret@example.test" in message_bytes(msg)
    wire = send_bytes(msg)
    assert b"secret@example.test" not in wire
    assert b"Bcc" not in wire and b"bcc" not in wire
    assert b"c@example.test" in wire  # Cc stays visible
    assert envelope_recipients(msg) == ["a@example.test", "c@example.test", "secret@example.test"]
    assert msg["Bcc"] is not None  # the original message is not mutated


def test_send_bytes_has_no_bcc_even_when_only_bcc():
    msg = build_message(ME, [], "s", "b", bcc=["only@example.test"])
    assert b"only@example.test" not in send_bytes(msg)
    assert envelope_recipients(msg) == ["only@example.test"]


# ------------------------------------------------------------------ replies


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Vize", "Re: Vize"),
        ("Re: Vize", "Re: Vize"),
        ("RE: Re: re:  Vize", "Re: Vize"),
        ("Ynt: Re: Vize sonuçları", "Re: Vize sonuçları"),
        ("YNT: Vize", "Re: Vize"),
        ("Re[2]: Vize", "Re: Vize"),
        ("Revize notları", "Re: Revize notları"),
        ("", "Re:"),
    ],
)
def test_reply_subject_does_not_stack(subject, expected):
    assert reply_subject(subject) == expected


def test_reply_threading_reply_to_and_quote():
    orig = original("from_faculty.eml")
    msg = build_reply(orig, ME, "Teşekkürler hocam.\nİyi günler.")
    back = reparse(msg.as_bytes())
    assert back["Subject"] == "Re: Vize sonuçları"
    assert back["In-Reply-To"] == "<fx-17@fixtures.test>"
    assert back["References"] == "<fx-00@fixtures.test> <fx-16@fixtures.test> <fx-17@fixtures.test>"
    assert [a.addr_spec for a in back["To"].addresses] == ["bolum.test@bogazici.edu.tr"]  # Reply-To
    assert back["To"].addresses[0].display_name == "Bölüm Sekreteri"
    assert back["Cc"] is None
    text = back.get_content().replace("\r\n", "\n")
    assert text.startswith("Teşekkürler hocam.\nİyi günler.\n\nOn 2026-03-02T10:15:00+03:00, ")
    assert (
        "> Vize sonuçları yarın açıklanacaktır.\n> İtirazlar için bölüm sekreterliğine başvurun.\n"
        in text
    )
    assert msg["From"].addresses[0].addr_spec == ME


def test_reply_goes_to_from_when_no_reply_to():
    orig = original("from_student.eml")
    back = reparse(build_reply(orig, "kullanici.test@bogazici.edu.tr", "Tamam.").as_bytes())
    assert [a.addr_spec for a in back["To"].addresses] == ["ogrenci.test@std.bogazici.edu.tr"]
    assert back["To"].addresses[0].display_name == "Test Öğrenci"
    assert back["Cc"] is None and back["Bcc"] is None


def test_reply_all_excludes_self_and_dedupes():
    orig = original("from_faculty.eml")
    back = reparse(build_reply(orig, ME, "Hepinize.", reply_all=True).as_bytes())
    to = [a.addr_spec for a in back["To"].addresses]
    cc = [a.addr_spec for a in back["Cc"].addresses]
    assert to == ["bolum.test@bogazici.edu.tr"]
    assert cc == [
        "ogrenci.test@std.bogazici.edu.tr",
        "baska.test@std.bogazici.edu.tr",
        "asistan.test@bogazici.edu.tr",
    ]
    assert ME not in to + cc
    assert len(set(to + cc)) == len(to + cc)
    # self matching is case-insensitive and ignores the display name
    back = reparse(build_reply(orig, f"Ben <{ME.upper()}>", "x", reply_all=True).as_bytes())
    assert ME not in [a.addr_spec.lower() for h in ("To", "Cc") for a in back[h].addresses]


def test_reply_to_own_message_goes_to_its_recipients():
    orig = original("from_student.eml")
    back = reparse(build_reply(orig, orig.from_addr, "Ek bilgi.").as_bytes())
    assert [a.addr_spec for a in back["To"].addresses] == ["deneme.hoca@bogazici.edu.tr"]


def test_reply_without_message_id_and_with_long_references():
    orig = original("from_faculty.eml").model_copy(update={"message_id": None})
    back = reparse(build_reply(orig, ME, "x").as_bytes())
    assert (
        back["In-Reply-To"] is None
        and back["References"] == "<fx-00@fixtures.test> <fx-16@fixtures.test>"
    )
    refs = [f"<r{i}@x.test>" for i in range(40)]
    orig = original().model_copy(update={"references": refs})
    out = reparse(build_reply(orig, ME, "x").as_bytes())["References"].split()
    assert len(out) == 20 and out[0] == "<r0@x.test>" and out[-1] == "<fx-17@fixtures.test>"


def test_reply_to_hostile_original_cannot_inject_headers():
    hostile = parse_message((EML / "injection_plain.eml").read_bytes(), 9, "INBOX")
    msg = build_reply(hostile, ME, "Cevap.", reply_all=True)
    data = send_bytes(msg)
    back = reparse(data)
    assert back["Bcc"] is None  # the smuggled "Bcc:" stays inert subject text
    assert back["Subject"].startswith("Re: Ödev") and "\n" not in back["Subject"]
    assert "attacker@example.invalid" not in envelope_recipients(msg)


def test_quote_is_truncated():
    orig = original().model_copy(update={"text_body": "satır\n" * 5000})
    text = reparse(build_reply(orig, ME, "x", max_quote_chars=100).as_bytes()).get_content()
    assert text.count("> satır") <= 17 and "[...]" in text
