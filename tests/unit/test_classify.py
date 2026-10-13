"""Sender classification: precedence list > standard > student > faculty > external."""

from __future__ import annotations

from pathlib import Path

import pytest

from posta_mcp.classify import Rules, classify, classify_with_reason
from posta_mcp.config import preview_config
from posta_mcp.mime import parse_message
from posta_mcp.models import MessageSummary

EML = Path(__file__).resolve().parent.parent / "fixtures" / "eml"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.from_config(preview_config("bogazici", "kullanici.test@std.bogazici.edu.tr"))


def msg(addr: str, *, to=(), cc=(), list_id=None) -> MessageSummary:
    return MessageSummary(
        uid=1, folder="INBOX", from_addr=addr, to=list(to), cc=list(cc), list_id=list_id
    )


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [("from_student", "student"), ("from_faculty", "faculty"), ("mailing_list", "list")],
)
def test_fixtures(rules, fixture, expected):
    parsed = parse_message((EML / f"{fixture}.eml").read_bytes(), 1, "INBOX")
    assert classify(parsed, rules) == expected


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("ogrenci@std.bogazici.edu.tr", "student"),
        ("OGRENCI@STD.BOGAZICI.EDU.TR", "student"),
        ("hoca@bogazici.edu.tr", "faculty"),
        ("Hoca@BOGAZICI.edu.TR", "faculty"),
        ("hoca@cmpe.bogazici.edu.tr", "faculty"),  # subdomain
        ("misafir@pt.bogazici.edu.tr", "faculty"),
        ("mezun@alum.bogazici.edu.tr", "faculty"),  # per the preset's institution list
        ("eski@boun.edu.tr", "faculty"),
        ("kisi@gmail.com", "external"),
        ("hoca@evilbogazici.edu.tr", "external"),  # not a subdomain
        ("hoca@bogazici.edu.tr.evil.example", "external"),
        ("", "external"),
    ],
)
def test_sender_domains(rules, addr, expected):
    assert classify(msg(addr), rules) == expected


@pytest.mark.parametrize(
    "addr",
    [
        "noreply@bogazici.edu.tr",
        "No-Reply@Bogazici.edu.tr",
        "no_reply@bogazici.edu.tr",
        "donotreply@bogazici.edu.tr",
        "do-not-reply@std.bogazici.edu.tr",
        "mailer-daemon@bogazici.edu.tr",
        "postmaster@std.bogazici.edu.tr",
        "noreply+ab12@bogazici.edu.tr",
        "bilgiislem@bogazici.edu.tr",
        "BilgiIslem@Bogazici.edu.tr",
    ],
)
def test_standard(rules, addr):
    assert classify(msg(addr), rules) == "standard"


def test_automated_name_outside_the_institution_is_external(rules):
    assert classify(msg("noreply@shop.example"), rules) == "external"
    assert classify(msg("mailer-daemon@gmail.com"), rules) == "external"


def test_list_id_wins_over_everything(rules):
    # a faculty member posting through a list, and an automated sender with a List-Id
    assert classify(msg("hoca@bogazici.edu.tr", list_id="<x.list.bogazici.edu.tr>"), rules) == (
        "list"
    )
    assert classify(msg("noreply@bogazici.edu.tr", list_id="<x>"), rules) == "list"
    assert classify(msg("kisi@gmail.com", list_id="<x>"), rules) == "list"


def test_list_by_address_or_domain(rules):
    assert classify(msg("students@list.bogazici.edu.tr"), rules) == "list"
    assert classify(msg("kulup@list.bogazici.edu.tr"), rules) == "list"  # *@list domain
    to_list = msg("hoca@bogazici.edu.tr", to=["Students <Students@List.Bogazici.edu.tr>"])
    assert classify(to_list, rules) == "list"
    cc_list = msg("ogrenci@std.bogazici.edu.tr", cc=["instructor@list.bogazici.edu.tr"])
    assert classify(cc_list, rules) == "list"


def test_precedence_standard_over_student_and_faculty(rules):
    assert classify(msg("noreply@std.bogazici.edu.tr"), rules) == "standard"
    assert classify(msg("bilgiislem@bogazici.edu.tr"), rules) == "standard"


def test_reason_names_the_rule_not_the_message(rules):
    c = classify_with_reason(msg("ogrenci@std.bogazici.edu.tr"), rules)
    assert c.category == "student" and "student domain" in c.reason
    assert "ogrenci" not in c.reason  # never quotes message content
    assert str(c) == "student"
    assert "List-Id" in classify_with_reason(msg("a@b.c", list_id="<x>"), rules).reason


def test_empty_rules_classify_everything_external():
    assert classify(msg("hoca@bogazici.edu.tr"), Rules()) == "external"


def test_rules_from_config_is_defensive():
    class Bare:  # a config without classification fields
        pass

    assert Rules.from_config(Bare()) == Rules()
    assert classify(msg("hoca@bogazici.edu.tr"), Rules.from_config(Bare())) == "external"


def test_rules_are_case_insensitive_and_user_overridable():
    rules = Rules.build(
        list_addresses=["Duyuru@Kulup.EXAMPLE"],
        student_domains=["@Ogr.Example"],
        institution_domains=["Okul.Example"],
        standard_local_parts=["No-Reply"],
    )
    assert classify(msg("duyuru@kulup.example"), rules) == "list"
    assert classify(msg("a@ogr.example"), rules) == "student"
    assert classify(msg("a@x.okul.example"), rules) == "faculty"
    assert classify(msg("noreply@okul.example"), rules) == "standard"
