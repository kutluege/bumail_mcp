"""Sending against GreenMail over real SMTPS (verified TLS with the test CA)."""

from __future__ import annotations

import time
from email import message_from_bytes, policy

import pytest
from mcp import Client
from mcp.types import ElicitResult

from posta_mcp import compose, smtp
from posta_mcp.config import load_config
from posta_mcp.imap import Mailbox
from posta_mcp.server import build_server

TR_SUBJECT = "Toplantı duyurusu ğüşıöç İıĞ"
TR_BODY = "Merhaba,\n\nYarınki toplantı için salon ayrıldı: Çalışma Odası 2. İyi çalışmalar.\n"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def config_for(posta_env, tmp_path, monkeypatch):
    def make(user=None, **overrides):
        # No config file: everything comes from the POSTA_* variables.
        env = posta_env(user, **overrides)
        monkeypatch.setenv("POSTA_PASSWORD", env["POSTA_PASSWORD"])  # read from os.environ
        return load_config(env=env, path=tmp_path / "none.toml")

    return make


def _imap_available(config) -> str | None:
    """None when the Mailbox implementation works, else a skip reason."""
    try:
        with Mailbox(config):
            return None
    except NotImplementedError:
        return "posta_mcp.imap.Mailbox is not implemented in this branch yet"
    except Exception as exc:  # noqa: BLE001
        return f"Mailbox could not connect: {type(exc).__name__}"


class Asker:
    def __init__(self, action="accept", confirm=True):
        self.action, self.confirm, self.messages = action, confirm, []

    async def __call__(self, ctx, params):
        self.messages.append(params.message)
        if self.action == "accept":
            return ElicitResult(action="accept", content={"confirm": self.confirm})
        return ElicitResult(action=self.action)


def _wait_for_mail(imap_client, user, count=1, timeout=10):
    c = imap_client(user)
    deadline = time.time() + timeout
    while True:
        c.select_folder("INBOX")
        uids = c.search("ALL")
        if len(uids) >= count or time.time() > deadline:
            return c, uids
        time.sleep(0.2)


def _raw(c, uid) -> bytes:
    return c.fetch([uid], [b"BODY.PEEK[]"])[uid][b"BODY[]"]


ARGS_TEMPLATE = {"subject": TR_SUBJECT, "body": TR_BODY}


def test_smtp_send_over_smtps_delivers_turkish_utf8_and_hides_bcc(
    greenmail, config_for, imap_client
):
    config = config_for()
    msg = compose.build_message(
        greenmail.student,
        [greenmail.faculty],
        TR_SUBJECT,
        TR_BODY,
        bcc=[greenmail.outsider],
    )
    assert smtp.send(config, msg) == []

    c, uids = _wait_for_mail(imap_client, greenmail.faculty)
    assert len(uids) == 1
    parsed = message_from_bytes(_raw(c, uids[0]), policy=policy.default)
    assert parsed["Subject"] == TR_SUBJECT
    assert parsed.get_body(("plain",)).get_content().replace("\r\n", "\n") == TR_BODY
    assert parsed["Bcc"] is None

    c2, uids2 = _wait_for_mail(imap_client, greenmail.outsider)  # the Bcc recipient got it
    assert len(uids2) == 1
    assert message_from_bytes(_raw(c2, uids2[0]), policy=policy.default)["Bcc"] is None


def test_smtp_wrong_login_is_a_secret_free_error(greenmail, config_for, monkeypatch):
    config = config_for(POSTA_PASSWORD="definitely-wrong-fake-pw")
    monkeypatch.setenv("POSTA_PASSWORD", "definitely-wrong-fake-pw")
    msg = compose.build_message(greenmail.student, [greenmail.faculty], "x", "y")
    with pytest.raises(smtp.SmtpError) as info:
        smtp.send(config, msg)
    assert "definitely-wrong-fake-pw" not in str(info.value)


def test_smtp_untrusted_certificate_fails_verification(greenmail, config_for):
    config = config_for(POSTA_CA_FILE=None)  # system CAs only: the test CA is not trusted
    config = config.model_copy(update={"ca_file": None})
    msg = compose.build_message(greenmail.student, [greenmail.faculty], "x", "y")
    with pytest.raises(smtp.SmtpError, match="TLS"):
        smtp.send(config, msg)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_send_message_tool_end_to_end(mode, greenmail, config_for, imap_client, seed_message):
    config = config_for(POSTA_ALLOW_SEND="1", POSTA_SAVE_SENT=None)
    asker = Asker()
    args = {"to": [greenmail.faculty], "cc": [], "bcc": [greenmail.outsider], **ARGS_TEMPLATE}
    async with Client(build_server(config), mode=mode, elicitation_callback=asker) as c:
        result = await c.call_tool("send_message", args)
    # Without a working Mailbox the SMTP send still succeeds and the Sent copy is a warning.
    assert not result.is_error and result.structured_content["status"] == "sent"
    assert len(asker.messages) == 1 and greenmail.outsider in asker.messages[0]

    inbox, uids = _wait_for_mail(imap_client, greenmail.faculty)
    assert len(uids) == 1
    parsed = message_from_bytes(_raw(inbox, uids[0]), policy=policy.default)
    assert parsed["Subject"] == TR_SUBJECT
    assert parsed["Bcc"] is None
    assert parsed["From"].addresses[0].addr_spec == greenmail.student
    assert parsed.get_body(("plain",)).get_content().replace("\r\n", "\n") == TR_BODY


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_declined_send_delivers_nothing(mode, greenmail, config_for, imap_client):
    config = config_for(POSTA_ALLOW_SEND="1")
    args = {"to": [greenmail.faculty], **ARGS_TEMPLATE}
    async with Client(build_server(config), mode=mode, elicitation_callback=Asker("decline")) as c:
        result = await c.call_tool("send_message", args)
    assert result.structured_content["status"] == "not_sent"
    time.sleep(1)
    _c, uids = _wait_for_mail(imap_client, greenmail.faculty, timeout=0.5)
    assert uids == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_sending_disabled_by_default_delivers_nothing(
    mode, greenmail, config_for, imap_client
):
    config = config_for(POSTA_ALLOW_SEND=None)
    assert not config.send_enabled
    args = {"to": [greenmail.faculty], **ARGS_TEMPLATE}
    async with Client(build_server(config), mode=mode, elicitation_callback=Asker()) as c:
        result = await c.call_tool("send_message", args)
    assert result.is_error
    _c, uids = _wait_for_mail(imap_client, greenmail.faculty, timeout=0.5)
    assert uids == []


class ImapAdapter:
    """Test-only stand-in for the parts of ``Mailbox`` that sending uses, over imapclient.

    Lets the Sent-copy / draft flows run against GreenMail even where the product ``Mailbox`` is
    unavailable.  (COPY + \\Deleted + EXPUNGE here is throwaway test-server code.)
    """

    FOLDERS = {"sent": "Sent", "drafts": "Drafts", "trash": "Trash"}

    def __init__(self, greenmail, user):
        self.gm, self.user = greenmail, user

    def __call__(self):
        return self

    def __enter__(self):
        import imapclient

        self.c = imapclient.IMAPClient(
            self.gm.host,
            port=self.gm.imaps_port,
            ssl=True,
            ssl_context=self.gm.ssl_context(),
            timeout=30,
        )
        self.c.login(self.user, self.gm.password(self.user))
        return self

    def __exit__(self, *exc):
        self.c.logout()

    def special_folder(self, role):
        return self.FOLDERS[role]

    def fetch_raw(self, folder, uid):
        self.c.select_folder(folder, readonly=True)
        return self.c.fetch([uid], [b"BODY.PEEK[]"])[uid][b"BODY[]"]

    def append(self, folder, raw, flags=()):
        self.c.append(folder, raw, flags=list(flags))

    def move(self, folder, uids, destination):
        self.c.select_folder(folder)
        self.c.copy(uids, destination)
        self.c.add_flags(uids, [b"\\Deleted"])
        self.c.expunge()


@pytest.fixture(params=["adapter", "mailbox"])
def mailbox_for(request, greenmail, config_for):
    """(config, server mailbox factory or None for the product Mailbox); skips if unavailable."""

    def make(**overrides):
        config = config_for(POSTA_ALLOW_SEND="1", **overrides)
        if request.param == "adapter":
            return config, ImapAdapter(greenmail, greenmail.student)
        if reason := _imap_available(config):
            pytest.skip(reason)
        return config, None

    return make


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_sent_copy_is_appended_with_seen(mode, greenmail, mailbox_for, imap_client):
    config, factory = mailbox_for()
    student = imap_client(greenmail.student)
    student.create_folder("Sent")
    args = {"to": [greenmail.faculty], "bcc": [greenmail.outsider], **ARGS_TEMPLATE}
    async with Client(
        build_server(config, mailbox=factory), mode=mode, elicitation_callback=Asker()
    ) as c:
        result = await c.call_tool("send_message", args)
    assert result.structured_content["saved_to_sent"] is True, result.structured_content
    student.select_folder("Sent")
    (uid,) = student.search("ALL")
    data = student.fetch([uid], [b"BODY.PEEK[]", b"FLAGS"])[uid]
    assert b"\\Seen" in data[b"FLAGS"]
    parsed = message_from_bytes(data[b"BODY[]"], policy=policy.default)
    assert parsed["Subject"] == TR_SUBJECT


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_send_draft_end_to_end(mode, greenmail, mailbox_for, imap_client, seed_message):
    config, factory = mailbox_for()
    draft = compose.build_message(
        greenmail.student, [greenmail.faculty], TR_SUBJECT, TR_BODY, bcc=[greenmail.outsider]
    )
    uid = seed_message(
        greenmail.student, compose.message_bytes(draft), folder="Drafts", flags=(b"\\Draft",)
    )
    student = imap_client(greenmail.student)
    student.create_folder("Trash")
    student.create_folder("Sent")

    asker = Asker()
    async with Client(
        build_server(config, mailbox=factory), mode=mode, elicitation_callback=asker
    ) as c:
        result = await c.call_tool("send_draft", {"uid": uid})
    assert result.structured_content["status"] == "sent", result.content[0].text
    assert greenmail.outsider in asker.messages[0]

    inbox, uids = _wait_for_mail(imap_client, greenmail.faculty)
    parsed = message_from_bytes(_raw(inbox, uids[0]), policy=policy.default)
    assert parsed["Subject"] == TR_SUBJECT and parsed["Bcc"] is None
    assert parsed.get_body(("plain",)).get_content().replace("\r\n", "\n") == TR_BODY
    _c2, bcc_uids = _wait_for_mail(imap_client, greenmail.outsider)
    assert len(bcc_uids) == 1

    # Draft moved to Trash (not deleted), copy in Sent.
    student.select_folder("Drafts")
    assert student.search("ALL") == []
    student.select_folder("Trash")
    assert len(student.search("ALL")) == 1
    student.select_folder("Sent")
    assert len(student.search("ALL")) == 1
