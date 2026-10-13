"""hoca-ozet, ogrenci-ozet and bana-ozet end to end: MCP server -> IMAP -> GreenMail."""

from __future__ import annotations

import pytest
from mcp_call import eml

SEEN = b"\\Seen"

# The Boğaziçi classification rules (as in the preset) for a generic-preset test account.
RULES_TOML = """\
[domains]
institution = ["bogazici.edu.tr", "boun.edu.tr"]
student = ["std.bogazici.edu.tr"]

[classification]
list_addresses = ["students@list.bogazici.edu.tr"]
list_domains = ["list.bogazici.edu.tr"]
standard_addresses = ["bilgiislem@bogazici.edu.tr"]
standard_local_parts = ["noreply", "no-reply", "mailer-daemon", "postmaster"]
"""

ANNOUNCEMENT = (
    "From: Bilgi Islem <bilgiislem@bogazici.edu.tr>\r\n"
    "To: ogrenci@std.bogazici.test\r\n"
    "Subject: =?utf-8?B?R8O8bmV5IEthbXDDvHMgeWVtZWtoYW5lc2k=?=\r\n"  # Güney Kampüs yemekhanesi
    "Date: Mon, 12 Oct 2026 09:00:00 +0300\r\n"
    "Message-ID: <ann-1@fixtures.test>\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "Content-Transfer-Encoding: 8bit\r\n"
    "\r\n"
    "Güney Kampüs yemekhanesi 15-16 Ekim'de bakım nedeniyle kapalıdır.\r\n"
).encode()

TENNIS = (
    "From: Spor Birimi <hoca@bogazici.edu.tr>\r\n"
    "To: ogrenci@std.bogazici.test\r\n"
    "Subject: Kortlar\r\n"
    "Date: Mon, 12 Oct 2026 09:30:00 +0300\r\n"
    "Message-ID: <ann-2@fixtures.test>\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "Content-Transfer-Encoding: 8bit\r\n"
    "\r\n"
    "TENİSÇİLER: kortlar bu hafta sonu bakımda.\r\n"
).encode()


@pytest.fixture
def digest_tool(tool, tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(RULES_TOML, encoding="utf-8")
    monkeypatch.setenv("POSTA_PROFILE", str(tmp_path / "profile.toml"))
    tool.use(POSTA_CONFIG=str(cfg))

    def call(name: str, **args):
        out = tool(name, **args)
        if out.is_error and "NotImplementedError" in out.text:
            pytest.skip("imap.Mailbox is not implemented yet (NotImplementedError)")
        assert not out.is_error, out.text
        return out.text

    return call


@pytest.fixture
def seeded(seed_message, greenmail):
    uids = {
        "student": seed_message(greenmail.student, eml("from_student.eml")),
        "faculty": seed_message(greenmail.student, eml("from_faculty.eml")),
        "list": seed_message(greenmail.student, eml("mailing_list.eml")),
        "standard": seed_message(greenmail.student, ANNOUNCEMENT),
        "tennis": seed_message(greenmail.student, TENNIS),
    }
    return uids


def test_hoca_and_ogrenci_ozet(digest_tool, seeded):
    hoca = digest_tool("hoca_ozet", days=7)
    assert "Vize sonuçları" in hoca and "Kortlar" in hoca
    assert "Ödev teslimi" not in hoca and "Yemekhane duyurusu" not in hoca
    assert "<untrusted-email" in hoca and "not instructions" in hoca

    ogr = digest_tool("ogrenci_ozet", days=7)
    assert "Hocam merhaba" in ogr and "Vize sonuçları" not in ogr


def test_bana_ozet_with_profile(digest_tool, seeded):
    guidance = digest_tool("bana_ozet")
    assert "update_profile" in guidance and "<untrusted-email nonce" not in guidance

    digest_tool(
        "update_profile",
        add_interests=[{"name": "Güney Kampüs"}, {"name": "yemekhane"}, {"name": "tenis"}],
    )
    out = digest_tool("bana_ozet", days=7)
    assert "kapalıdır" in out  # the standard-sender announcement
    assert "yemekhanesi bakım" in out or "bakım nedeniyle" in out
    assert "matches your interests: Güney Kampüs, yemekhane" in out
    assert "TENİSÇİLER" in out and "matches your interests: tenis" in out  # tenisçi via suffix
    assert "Hocam merhaba" not in out  # student mail is listed by subject only
    assert "Ödev" in out and 'kind="subject-list"' in out


def test_digest_tools_do_not_set_seen(digest_tool, seeded, greenmail, imap_client):
    digest_tool("update_profile", add_interests=[{"name": "yemekhane"}])
    digest_tool("hoca_ozet")
    digest_tool("ogrenci_ozet")
    digest_tool("bana_ozet")
    c = imap_client(greenmail.student)
    c.select_folder("INBOX", readonly=True)
    for name, uid in seeded.items():
        assert SEEN not in c.get_flags([uid])[uid], name
