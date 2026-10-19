"""Non-negotiable #4: the send gate. In-memory MCP client on both protocol eras (RESEARCH 1.4)."""

from __future__ import annotations

import hashlib
import re
import smtplib
from contextlib import contextmanager
from email import message_from_bytes, policy

import pytest
from mcp import Client
from mcp.types import ElicitResult

from posta_mcp import compose
from posta_mcp.config import Config
from posta_mcp.server import build_server

PASSWORD = "fake-pw-Lm3!send-unit"
ME = "me@example.org"
TR_SUBJECT = "Toplantı ğüşıöç İıĞ"
TR_BODY = "Merhaba,\n\nYarınki toplantı için salon ayrıldı: Çalışma Odası 2. İyi çalışmalar.\n"
MODES = ["legacy", "auto"]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


class FakeSMTP:
    """Class-level journal so we can see whether SMTP was touched at all."""

    sent: list[dict] = []
    constructed = 0

    def __init__(self, *a, **kw):
        type(self).constructed += 1

    def login(self, user, password):
        pass

    def sendmail(self, sender, recipients, data):
        type(self).sent.append({"sender": sender, "recipients": list(recipients), "data": data})
        return {}

    def ehlo(self):
        return 250, b""

    def has_extn(self, name):
        return True

    def starttls(self, context=None):
        pass

    def quit(self):
        pass

    def close(self):
        pass


class FakeMailbox:
    def __init__(self, drafts: dict[int, bytes] | None = None, originals: dict | None = None):
        self.drafts = drafts or {}
        self.originals = originals or {}
        self.appended: list[tuple] = []
        self.moved: list[tuple] = []
        self.fail_append = False

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ensure_special_folder(self, role):
        return self.special_folder(role)

    def special_folder(self, role):
        return {"sent": "Sent", "drafts": "Drafts", "trash": "Trash"}[role]

    def fetch_raw(self, folder, uid):
        assert folder == "Drafts"
        return self.drafts[uid]

    def fetch(self, folder, uid, mark_seen=False):
        return self.originals[(folder, uid)]

    def append(self, folder, raw, flags=()):
        if self.fail_append:
            raise RuntimeError("quota exceeded")
        self.appended.append((folder, raw, tuple(flags)))
        return 1

    def move(self, folder, uids, destination):
        self.moved.append((folder, list(uids), destination))


def make_config(*, enabled=True, save_sent="auto", security="ssl") -> Config:
    return Config.model_validate(
        {
            "email": ME,
            "username": "me-login",
            "imap": {"host": "imap.example.org", "port": 993},
            "smtp": {"host": "smtp.example.org", "port": 465, "security": security},
            "sending": {"enabled": enabled},
            "save_sent": save_sent,
        }
    )


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent = []
    FakeSMTP.constructed = 0
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("POSTA_PASSWORD", PASSWORD)
    return FakeSMTP


class Asker:
    """Elicitation callback: answers with `action` and, on accept, `confirm`."""

    def __init__(self, action="accept", confirm=True):
        self.action, self.confirm = action, confirm
        self.messages: list[str] = []

    async def __call__(self, ctx, params):
        self.messages.append(params.message)
        if self.action == "accept":
            return ElicitResult(action="accept", content={"confirm": self.confirm})
        return ElicitResult(action=self.action)


@contextmanager
def _noop():
    yield


def client_for(config, mailbox, mode, asker):
    server = build_server(config, mailbox=mailbox)
    kwargs = {"elicitation_callback": asker} if asker is not None else {}
    return Client(server, mode=mode, **kwargs)


ARGS = {
    "to": ["Ali Yılmaz <ali@example.org>"],
    "cc": ["veli@example.org"],
    "bcc": ["gizli@example.org"],
    "subject": TR_SUBJECT,
    "body": TR_BODY,
}


def text(result) -> str:
    return result.content[0].text


def assert_nothing_sent(mailbox=None):
    assert FakeSMTP.sent == []
    assert FakeSMTP.constructed == 0
    if mailbox is not None:
        assert mailbox.appended == [] and mailbox.moved == []


# --- gate: disabled / no elicitation ---------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_disabled_refuses_and_never_touches_smtp_or_asks(mode):
    mb, asker = FakeMailbox(), Asker()
    async with client_for(make_config(enabled=False), mb, mode, asker) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert r.is_error
    t = text(r)
    assert "disabled" in t and "POSTA_ALLOW_SEND" in t and "taslak_olustur" in t
    assert asker.messages == []  # not even asked
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_disabled_send_draft_refuses(mode):
    mb, asker = FakeMailbox(), Asker()
    async with client_for(make_config(enabled=False), mb, mode, asker) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 1})
    assert r.is_error and "disabled" in text(r)
    assert asker.messages == []
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_enabled_but_client_cannot_elicit_refuses(mode):
    mb = FakeMailbox()
    async with client_for(make_config(), mb, mode, None) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert r.is_error and "draft" in text(r).lower() and "NOT sent" in text(r)
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_without_elicitation_refuses(mode):
    mb = FakeMailbox(drafts={1: compose.message_bytes(_draft_msg())})
    async with client_for(make_config(), mb, mode, None) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 1})
    assert r.is_error and "draft" in text(r).lower()
    assert_nothing_sent(mb)


# --- decline / cancel / confirm=false ---------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "action,confirm,word",
    [("decline", True, "declined"), ("cancel", True, "cancelled"), ("accept", False, "checkbox")],
)
async def test_not_confirmed_sends_nothing(mode, action, confirm, word):
    mb, asker = FakeMailbox(), Asker(action, confirm)
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert not r.is_error
    assert r.structured_content["status"] == "not_sent"
    assert "NOT SENT" in text(r) and word in text(r)
    assert len(asker.messages) == 1
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("action,confirm", [("decline", True), ("cancel", True), ("accept", False)])
async def test_draft_not_confirmed_sends_nothing_and_keeps_draft(mode, action, confirm):
    mb = FakeMailbox(drafts={7: compose.message_bytes(_draft_msg())})
    async with client_for(make_config(), mb, mode, Asker(action, confirm)) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.structured_content["status"] == "not_sent"
    assert_nothing_sent(mb)


# --- accept ------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_accept_sends_once_and_bytes_match_the_shown_fingerprint(mode):
    mb, asker = FakeMailbox(), Asker()
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert not r.is_error and r.structured_content["status"] == "sent"
    assert len(FakeSMTP.sent) == 1 and len(asker.messages) == 1
    sent = FakeSMTP.sent[0]

    shown = asker.messages[0]
    fingerprint = re.search(r"Fingerprint[^:]*: ([0-9a-f]{64})", shown).group(1)
    assert hashlib.sha256(sent["data"]).hexdigest() == fingerprint

    # Bcc: in the envelope, absent from the bytes.
    assert set(sent["recipients"]) == {
        "ali@example.org",
        "veli@example.org",
        "gizli@example.org",
    }
    assert sent["sender"] == ME
    assert b"gizli@example.org" not in sent["data"] and b"Bcc:" not in sent["data"]

    # Turkish round-trips as UTF-8.
    parsed = message_from_bytes(sent["data"], policy=policy.default)
    assert parsed["Subject"] == TR_SUBJECT
    got = parsed.get_body(("plain",)).get_content().replace("\r\n", "\n")
    assert got.rstrip() == TR_BODY.rstrip()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_confirmation_shows_recipients_subject_and_full_body(mode):
    asker = Asker("decline")
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        await c.call_tool("posta_gonder", ARGS)
    shown = asker.messages[0]
    for needle in (
        ME,
        "ali@example.org",
        "Ali Yılmaz",
        "veli@example.org",
        "gizli@example.org",
        TR_SUBJECT,
        TR_BODY.strip(),
        "Attachments: none",
    ):
        assert needle in shown, needle
    assert "Bcc:" in shown


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_long_body_is_truncated_in_the_dialog_with_length_but_sent_in_full(mode):
    body = "x" * 9000 + "SONUN-SONU"
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        await c.call_tool("posta_gonder", {**ARGS, "body": body})
    shown = asker.messages[0]
    assert "x" * 4000 in shown and "x" * 4001 not in shown
    assert "SONUN-SONU" not in shown and "of 9011 characters" in shown
    data = FakeSMTP.sent[0]["data"]
    got = message_from_bytes(data, policy=policy.default).get_body(("plain",)).get_content()
    assert got.rstrip().endswith("SONUN-SONU")


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_sent_copy_is_appended_seen_and_includes_bcc(mode):
    mb = FakeMailbox()
    async with client_for(make_config(save_sent="auto"), mb, mode, Asker()) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert r.structured_content["saved_to_sent"] is True
    folder, raw, flags = mb.appended[0]
    assert folder == "Sent" and flags == (r"\Seen",)
    assert b"gizli@example.org" in raw


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_save_sent_never_skips_the_copy(mode):
    mb = FakeMailbox()
    async with client_for(make_config(save_sent="never"), mb, mode, Asker()) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert r.structured_content["status"] == "sent" and mb.appended == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_failing_sent_copy_is_a_warning_not_a_failure(mode):
    mb = FakeMailbox()
    mb.fail_append = True
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert not r.is_error and r.structured_content["status"] == "sent"
    assert len(FakeSMTP.sent) == 1
    assert any("Sent" in w for w in r.structured_content["warnings"])


# --- a model cannot bypass the gate -------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "extra",
    [
        {"confirm": True},
        {"decision": {"action": "accept", "data": {"confirm": True}}},
        {"decision": {"confirm": True}},
        {"send_enabled": True, "sending": {"enabled": True}},
    ],
)
async def test_extra_arguments_cannot_bypass_confirmation(mode, extra):
    mb, asker = FakeMailbox(), Asker("decline")
    async with client_for(make_config(), mb, mode, asker) as c:
        try:
            r = await c.call_tool("posta_gonder", {**ARGS, **extra})
        except Exception:  # noqa: BLE001 - rejecting the call outright is fine too
            r = None
    assert r is None or r.is_error or r.structured_content["status"] == "not_sent"
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_extra_arguments_do_not_bypass_when_client_cannot_elicit(mode):
    async with client_for(make_config(), FakeMailbox(), mode, None) as c:
        try:
            r = await c.call_tool("posta_gonder", {**ARGS, "confirm": True})
        except Exception:  # noqa: BLE001
            r = None
    assert r is None or r.is_error
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_input_schema_does_not_expose_decision_or_a_way_to_enable_sending(mode):
    async with client_for(make_config(), FakeMailbox(), mode, Asker()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    for name in ("posta_gonder", "taslagi_gonder"):
        props = tools[name].input_schema["properties"]
        assert "decision" not in props and "confirm" not in props
        assert tools[name].annotations.open_world_hint is True
    assert not any("enable" in n or "allow" in n for n in tools)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_invalid_recipient_is_refused_before_asking(mode):
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        r = await c.call_tool("posta_gonder", {**ARGS, "to": ["not an address"]})
    assert r.is_error and asker.messages == []
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_header_injection_in_subject_is_refused(mode):
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        r = await c.call_tool("posta_gonder", {**ARGS, "subject": "hi\r\nBcc: evil@example.org"})
    assert r.is_error and asker.messages == []
    assert_nothing_sent()


# --- hash binding --------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_message_mutated_after_it_was_shown_is_not_sent(mode, monkeypatch):
    """Tamper with the parked message while the user is looking at the dialog."""
    from posta_mcp.tools import send as send_mod

    parked: list = []
    real_get = send_mod._Store.get_or_create

    def spy(self, key, make):
        prepared = real_get(self, key, make)
        parked.append(prepared)
        return prepared

    monkeypatch.setattr(send_mod._Store, "get_or_create", spy)

    class Tamper(Asker):
        async def __call__(self, ctx, params):
            parked[-1].msg.replace_header("Subject", "TAMPERED")
            return await super().__call__(ctx, params)

    async with client_for(make_config(), FakeMailbox(), mode, Tamper()) as c:
        r = await c.call_tool("posta_gonder", ARGS)
    assert r.is_error and "changed" in text(r)
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_each_confirmation_sends_exactly_once(mode):
    async with client_for(make_config(), FakeMailbox(), mode, Asker()) as c:
        await c.call_tool("posta_gonder", ARGS)
        await c.call_tool("posta_gonder", ARGS)  # a second call needs its own confirmation
    assert len(FakeSMTP.sent) == 2
    assert FakeSMTP.sent[0]["data"] != FakeSMTP.sent[1]["data"]  # fresh Message-ID each time


# --- replies ------------------------------------------------------------------------------------


def _original():
    from posta_mcp.models import ParsedMessage

    return ParsedMessage(
        uid=5,
        folder="INBOX",
        from_name="Hoca",
        from_addr="hoca@example.org",
        to=[ME],
        subject="Ödev teslimi",
        date="2026-10-01T10:00:00",
        message_id="<orig-1@example.org>",
        text_body="Ödevler Cuma günü teslim edilecek.\n",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_reply_threads_and_defaults_recipient_and_subject(mode):
    mb = FakeMailbox(originals={("INBOX", 5): _original()})
    asker = Asker()
    args = {"to": [], "subject": "", "body": "Teşekkürler hocam.", "reply_to_folder": "INBOX"}
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("posta_gonder", {**args, "reply_to_uid": 5})
    assert r.structured_content["status"] == "sent"
    sent = FakeSMTP.sent[0]
    assert sent["recipients"] == ["hoca@example.org"]
    parsed = message_from_bytes(sent["data"], policy=policy.default)
    assert parsed["Subject"] == "Re: Ödev teslimi"
    assert parsed["In-Reply-To"] == "<orig-1@example.org>"
    assert "Ödevler Cuma" in parsed.get_body(("plain",)).get_content()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_reply_all_with_several_recipients_is_sent_to_each(mode):
    original = _original().model_copy(update={"to": [ME, "a@example.org", "b@example.org"]})
    mb = FakeMailbox(originals={("INBOX", 5): original})
    args = {"to": [], "subject": "", "body": "Tamam.", "reply_to_folder": "INBOX"}
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("posta_gonder", {**args, "reply_to_uid": 5, "reply_all": True})
    assert r.structured_content["status"] == "sent", r
    assert sorted(FakeSMTP.sent[0]["recipients"]) == [
        "a@example.org",
        "b@example.org",
        "hoca@example.org",
    ]


def test_bare_elicitation_capability_counts_as_form_support():
    from types import SimpleNamespace

    from mcp.types import ClientCapabilities

    from posta_mcp.tools.send import _can_elicit

    def ctx(caps):
        return SimpleNamespace(client_capabilities=ClientCapabilities.model_validate(caps))

    assert _can_elicit(ctx({"elicitation": {}}))
    assert _can_elicit(ctx({"elicitation": {"form": {}}}))
    assert not _can_elicit(ctx({"elicitation": {"url": {}}}))
    assert not _can_elicit(ctx({}))


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_reply_folder_without_uid_is_refused(mode):
    async with client_for(make_config(), FakeMailbox(), mode, Asker()) as c:
        r = await c.call_tool("posta_gonder", {**ARGS, "reply_to_folder": "INBOX"})
    assert r.is_error
    assert_nothing_sent()


# --- taslagi_gonder --------------------------------------------------------------------------------


def _draft_msg(**kw):
    return compose.build_message(
        kw.get("sender", ME),
        ["ali@example.org"],
        "Taslak konu ğş",
        "Taslak gövde İıÖö\n",
        bcc=["gizli@example.org"],
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_end_to_end_moves_draft_to_trash_and_hides_bcc(mode):
    mb = FakeMailbox(drafts={7: compose.message_bytes(_draft_msg())})
    asker = Asker()
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.structured_content["status"] == "sent"
    assert r.structured_content["draft_moved_to_trash"] is True
    assert mb.moved == [("Drafts", [7], "Trash")]
    assert mb.appended[0][0] == "Sent"
    sent = FakeSMTP.sent[0]
    assert set(sent["recipients"]) == {"ali@example.org", "gizli@example.org"}
    assert b"gizli@example.org" not in sent["data"]
    shown = asker.messages[0]
    assert "gizli@example.org" in shown and "Taslak gövde İıÖö" in shown
    fingerprint = re.search(r"Fingerprint[^:]*: ([0-9a-f]{64})", shown).group(1)
    assert hashlib.sha256(sent["data"]).hexdigest() == fingerprint
    parsed = message_from_bytes(sent["data"], policy=policy.default)
    assert parsed["Subject"] == "Taslak konu ğş"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_from_other_address_is_refused(mode):
    other = _draft_msg(sender="someone.else@example.org")
    mb = FakeMailbox(drafts={7: compose.message_bytes(other)})
    asker = Asker()
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.is_error and asker.messages == []
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_shows_attachment_names(mode):
    msg = _draft_msg()
    msg.add_attachment(
        b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="rapor-çalışma.pdf"
    )
    mb = FakeMailbox(drafts={3: compose.message_bytes(msg)})
    asker = Asker("decline")
    async with client_for(make_config(), mb, mode, asker) as c:
        await c.call_tool("taslagi_gonder", {"uid": 3})
    assert "rapor-çalışma.pdf" in asker.messages[0]
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_failed_smtp_send_leaves_draft_and_sent_untouched(mode, monkeypatch):
    def boom(self, *a):
        raise smtplib.SMTPDataError(554, b"rejected")

    monkeypatch.setattr(FakeSMTP, "sendmail", boom)
    mb = FakeMailbox(drafts={7: compose.message_bytes(_draft_msg())})
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.is_error and "nothing was sent" in text(r)
    assert PASSWORD not in text(r)
    assert mb.moved == [] and mb.appended == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_rejected_smtp_login_carries_the_turkish_hint(mode, monkeypatch):
    def reject(self, *a):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials " + PASSWORD.encode())

    monkeypatch.setattr(FakeSMTP, "login", reject)
    mb = FakeMailbox(drafts={7: compose.message_bytes(_draft_msg())})
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.is_error and "Giriş başarısız" in text(r)
    assert "nothing was sent" in text(r) and PASSWORD not in text(r)


# --- what the dialog must disclose, and what it must refuse ------------------------------------


def _draft_with(**headers):
    msg = _draft_msg()
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    return compose.message_bytes(msg)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_shows_reply_to_and_names_of_other_headers(mode):
    raw = _draft_with(Reply_To="Baska <baska@example.net>", X_Mailer="evil-tool", X_Track="1")
    mb = FakeMailbox(drafts={7: raw})
    asker = Asker("decline")
    async with client_for(make_config(), mb, mode, asker) as c:
        await c.call_tool("taslagi_gonder", {"uid": 7})
    shown = asker.messages[0]
    assert "Reply-To: Baska <baska@example.net>" in shown
    assert "Other headers: X-Mailer, X-Track" in shown
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_confirmation_omits_reply_to_and_other_headers_when_absent(mode):
    asker = Asker("decline")
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        await c.call_tool("posta_gonder", ARGS)
    assert "Reply-To:" not in asker.messages[0] and "Other headers:" not in asker.messages[0]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "headers",
    [
        {"Resent_To": "x@evil.test"},
        {"Resent_From": ME},
        {"Sender": "boss@example.net"},
        {"Disposition_Notification_To": ME},
        {"Return_Receipt_To": ME},
    ],
)
async def test_send_draft_refuses_redirecting_headers(mode, headers):
    mb = FakeMailbox(drafts={7: _draft_with(**headers)})
    asker = Asker()
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.is_error and "Refusing to send" in text(r) and "nothing was sent" in text(r)
    assert asker.messages == []
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_allows_sender_equal_to_the_account(mode):
    mb = FakeMailbox(drafts={7: _draft_with(Sender=f"Ben <{ME}>")})
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert not r.is_error and r.structured_content["status"] == "sent"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("char", ["‮", "​", "⁦", "﻿"])
@pytest.mark.parametrize("where", ["subject", "body"])
async def test_invisible_characters_are_refused_not_silently_shown(mode, char, where):
    args = {**ARGS, where: ARGS[where] + f"x{char}y"}
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        r = await c.call_tool("posta_gonder", args)
    assert r.is_error and "invisible" in text(r) and asker.messages == []
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_invisible_character_in_a_draft_header_is_refused(mode):
    msg = _draft_msg()
    msg.replace_header("Subject", "Fatura‮txt.exe")
    mb = FakeMailbox(drafts={7: compose.message_bytes(msg)})
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("taslagi_gonder", {"uid": 7})
    assert r.is_error and "invisible" in text(r)
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_result_lists_only_plain_addresses_and_no_subject(mode):
    mb = FakeMailbox(originals={("INBOX", 5): _original()})
    mb.fail_append = True  # produces a warning
    args = {
        "to": [],
        "subject": "",
        "body": "Tamam.",
        "reply_to_folder": "INBOX",
        "reply_to_uid": 5,
    }
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("posta_gonder", args)
    result = r.structured_content
    assert result["recipients"] == ["hoca@example.org"]  # address only, no display name
    assert result["warnings"] and not any("Ödev teslimi" in w for w in result["warnings"])


def test_store_builds_outside_the_lock_and_keeps_the_first_entry():
    import threading

    from posta_mcp.tools.send import Prepared, _Store

    store = _Store()
    inside = threading.Event()
    release = threading.Event()
    results = []

    def prepared(tag):
        return Prepared(None, tag, (), b"", "", "", tag, 10**12)  # type: ignore[arg-type]

    def slow():
        inside.set()
        release.wait(5)
        return prepared("slow")

    t = threading.Thread(target=lambda: results.append(store.get_or_create("k", slow)))
    t.start()
    assert inside.wait(5)
    # While make() is still running (e.g. doing IMAP I/O) the store is not locked.
    assert store.pop("other") is None
    assert store.get_or_create("k", lambda: prepared("fast")).shown == "fast"  # this one won
    release.set()
    t.join(5)
    assert results[0].shown == "fast"  # the late builder adopts the existing entry
    assert store.pop("k").shown == "fast"


# --- attachments from this computer -------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.usefixtures("no_windows_attributes")
async def test_attachment_is_shown_with_its_path_and_sent(mode, tmp_path):
    photo = tmp_path / "çiçek.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake jpeg")
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        r = await c.call_tool("posta_gonder", {**ARGS, "attachments": [str(photo)]})
    assert r.structured_content["status"] == "sent"
    shown = asker.messages[0]
    assert "çiçek.jpg [image/jpeg, 13 bytes]" in shown
    assert f"Attached from this computer: {photo.resolve()}" in shown
    sent = message_from_bytes(FakeSMTP.sent[0]["data"], policy=policy.default)
    (part,) = sent.iter_attachments()
    assert part.get_filename() == "çiçek.jpg" and part.get_content() == photo.read_bytes()
    fingerprint = re.search(r"Fingerprint[^:]*: ([0-9a-f]{64})", shown).group(1)
    assert hashlib.sha256(FakeSMTP.sent[0]["data"]).hexdigest() == fingerprint


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.usefixtures("no_windows_attributes")
async def test_attachment_changed_after_confirmation_is_sent_as_shown(mode, tmp_path):
    note = tmp_path / "not.txt"
    note.write_text("ilk hali")

    class Editor(Asker):
        async def __call__(self, ctx, params):
            note.write_text("sonradan degisti")
            return await super().__call__(ctx, params)

    asker = Editor()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        await c.call_tool("posta_gonder", {**ARGS, "attachments": [str(note)]})
    assert b"sonradan" not in FakeSMTP.sent[0]["data"]
    assert len(asker.messages) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.usefixtures("no_windows_attributes")
async def test_hidden_or_missing_attachment_is_refused_before_asking(mode, tmp_path):
    secret = tmp_path / ".ssh" / "id_ed25519"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY")
    for path, word in ((secret, "hidden"), (tmp_path / "yok.pdf", "not found")):
        asker = Asker()
        async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
            r = await c.call_tool("posta_gonder", {**ARGS, "attachments": [str(path)]})
        assert r.is_error and word in text(r) and "PRIVATE KEY" not in text(r)
        assert asker.messages == []
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.usefixtures("no_windows_attributes")
async def test_create_draft_with_attachment(mode, tmp_path):
    pdf = tmp_path / "rapor.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    mb = FakeMailbox()
    async with client_for(make_config(enabled=False), mb, mode, None) as c:
        r = await c.call_tool(
            "taslak_olustur",
            {
                "to": ["a@example.org"],
                "subject": "Rapor",
                "body": "Ekte.",
                "attachments": [str(pdf)],
            },
        )
        bad = await c.call_tool(
            "taslak_olustur",
            {"to": ["a@example.org"], "subject": "x", "body": "x", "attachments": ["rapor.pdf"]},
        )
    assert not r.is_error and "NOT been sent" in text(r)
    (folder, raw, flags) = mb.appended[0]
    (part,) = message_from_bytes(raw, policy=policy.default).iter_attachments()
    assert folder == "Drafts" and part.get_filename() == "rapor.pdf"
    assert bad.is_error and "absolute" in text(bad) and len(mb.appended) == 1
    assert_nothing_sent()
