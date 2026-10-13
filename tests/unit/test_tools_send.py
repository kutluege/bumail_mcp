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
        r = await c.call_tool("send_message", ARGS)
    assert r.is_error
    t = text(r)
    assert "disabled" in t and "POSTA_ALLOW_SEND" in t and "create_draft" in t
    assert asker.messages == []  # not even asked
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_disabled_send_draft_refuses(mode):
    mb, asker = FakeMailbox(), Asker()
    async with client_for(make_config(enabled=False), mb, mode, asker) as c:
        r = await c.call_tool("send_draft", {"uid": 1})
    assert r.is_error and "disabled" in text(r)
    assert asker.messages == []
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_enabled_but_client_cannot_elicit_refuses(mode):
    mb = FakeMailbox()
    async with client_for(make_config(), mb, mode, None) as c:
        r = await c.call_tool("send_message", ARGS)
    assert r.is_error and "draft" in text(r).lower() and "NOT sent" in text(r)
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_send_draft_without_elicitation_refuses(mode):
    mb = FakeMailbox(drafts={1: compose.message_bytes(_draft_msg())})
    async with client_for(make_config(), mb, mode, None) as c:
        r = await c.call_tool("send_draft", {"uid": 1})
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
        r = await c.call_tool("send_message", ARGS)
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
        r = await c.call_tool("send_draft", {"uid": 7})
    assert r.structured_content["status"] == "not_sent"
    assert_nothing_sent(mb)


# --- accept ------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_accept_sends_once_and_bytes_match_the_shown_fingerprint(mode):
    mb, asker = FakeMailbox(), Asker()
    async with client_for(make_config(), mb, mode, asker) as c:
        r = await c.call_tool("send_message", ARGS)
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
        await c.call_tool("send_message", ARGS)
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
        await c.call_tool("send_message", {**ARGS, "body": body})
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
        r = await c.call_tool("send_message", ARGS)
    assert r.structured_content["saved_to_sent"] is True
    folder, raw, flags = mb.appended[0]
    assert folder == "Sent" and flags == (r"\Seen",)
    assert b"gizli@example.org" in raw


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_save_sent_never_skips_the_copy(mode):
    mb = FakeMailbox()
    async with client_for(make_config(save_sent="never"), mb, mode, Asker()) as c:
        r = await c.call_tool("send_message", ARGS)
    assert r.structured_content["status"] == "sent" and mb.appended == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_failing_sent_copy_is_a_warning_not_a_failure(mode):
    mb = FakeMailbox()
    mb.fail_append = True
    async with client_for(make_config(), mb, mode, Asker()) as c:
        r = await c.call_tool("send_message", ARGS)
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
            r = await c.call_tool("send_message", {**ARGS, **extra})
        except Exception:  # noqa: BLE001 - rejecting the call outright is fine too
            r = None
    assert r is None or r.is_error or r.structured_content["status"] == "not_sent"
    assert_nothing_sent(mb)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_extra_arguments_do_not_bypass_when_client_cannot_elicit(mode):
    async with client_for(make_config(), FakeMailbox(), mode, None) as c:
        try:
            r = await c.call_tool("send_message", {**ARGS, "confirm": True})
        except Exception:  # noqa: BLE001
            r = None
    assert r is None or r.is_error
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_input_schema_does_not_expose_decision_or_a_way_to_enable_sending(mode):
    async with client_for(make_config(), FakeMailbox(), mode, Asker()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    for name in ("send_message", "send_draft"):
        props = tools[name].input_schema["properties"]
        assert "decision" not in props and "confirm" not in props
        assert tools[name].annotations.open_world_hint is True
    assert not any("enable" in n or "allow" in n for n in tools)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_invalid_recipient_is_refused_before_asking(mode):
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        r = await c.call_tool("send_message", {**ARGS, "to": ["not an address"]})
    assert r.is_error and asker.messages == []
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_header_injection_in_subject_is_refused(mode):
    asker = Asker()
    async with client_for(make_config(), FakeMailbox(), mode, asker) as c:
        r = await c.call_tool("send_message", {**ARGS, "subject": "hi\r\nBcc: evil@example.org"})
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
        r = await c.call_tool("send_message", ARGS)
    assert r.is_error and "changed" in text(r)
    assert_nothing_sent()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_each_confirmation_sends_exactly_once(mode):
    async with client_for(make_config(), FakeMailbox(), mode, Asker()) as c:
        await c.call_tool("send_message", ARGS)
        await c.call_tool("send_message", ARGS)  # a second call needs its own confirmation
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
        r = await c.call_tool("send_message", {**args, "reply_to_uid": 5})
    assert r.structured_content["status"] == "sent"
    sent = FakeSMTP.sent[0]
    assert sent["recipients"] == ["hoca@example.org"]
    parsed = message_from_bytes(sent["data"], policy=policy.default)
    assert parsed["Subject"] == "Re: Ödev teslimi"
    assert parsed["In-Reply-To"] == "<orig-1@example.org>"
    assert "Ödevler Cuma" in parsed.get_body(("plain",)).get_content()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_reply_folder_without_uid_is_refused(mode):
    async with client_for(make_config(), FakeMailbox(), mode, Asker()) as c:
        r = await c.call_tool("send_message", {**ARGS, "reply_to_folder": "INBOX"})
    assert r.is_error
    assert_nothing_sent()


# --- send_draft --------------------------------------------------------------------------------


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
        r = await c.call_tool("send_draft", {"uid": 7})
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
        r = await c.call_tool("send_draft", {"uid": 7})
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
        await c.call_tool("send_draft", {"uid": 3})
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
        r = await c.call_tool("send_draft", {"uid": 7})
    assert r.is_error and "nothing was sent" in text(r)
    assert PASSWORD not in text(r)
    assert mb.moved == [] and mb.appended == []
