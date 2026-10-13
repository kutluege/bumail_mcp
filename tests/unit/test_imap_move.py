"""Non-negotiable 5: moves never issue a plain EXPUNGE; fallback is UID COPY + UID EXPUNGE <uids>."""

from __future__ import annotations

import pytest
from imap_fakes import FakeIMAPClient, factory, raw_message

from posta_mcp.config import load_config
from posta_mcp.imap import Mailbox, MailboxError, SearchCriteria

FULL = (b"IMAP4REV1", b"MOVE", b"UIDPLUS")
NO_MOVE = (b"IMAP4REV1", b"UIDPLUS")
NEITHER = (b"IMAP4REV1",)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("POSTA_PASSWORD", "fake-test-password")
    FakeIMAPClient.instances.clear()


def folders():
    return {
        "INBOX": {1: raw_message("one"), 2: raw_message("two"), 3: raw_message("three")},
        "Trash": {},
        "Archive": {},
    }


def mailbox(caps):
    config = load_config(
        env={
            "POSTA_PRESET": "generic",
            "POSTA_EMAIL": "me@example.test",
            "POSTA_IMAP_HOST": "imap.example.test",
            "POSTA_SMTP_HOST": "smtp.example.test",
        }
    )
    return Mailbox(config, client_factory=factory(caps=caps, folders=folders()))


def test_move_uses_uid_move_when_available():
    with mailbox(FULL) as mb:
        client = FakeIMAPClient.instances[-1]
        mb.move("INBOX", [1, 3], "Archive")
    assert ("move", ((1, 3), "Archive")) in client.calls
    assert not {"copy", "uid_expunge", "expunge", "add_flags"} & set(client.names())
    assert sorted(client.folders["INBOX"]) == [2] and sorted(client.folders["Archive"]) == [1, 3]


def test_fallback_when_move_missing_uses_copy_deleted_uid_expunge():
    with mailbox(NO_MOVE) as mb:
        client = FakeIMAPClient.instances[-1]
        client.flags[("INBOX", 2)] = {b"\\Deleted"}  # untargeted, already \Deleted
        mb.move("INBOX", [1], "Trash")
    names = client.names()
    assert "move" not in names and "expunge" not in names
    assert names.index("copy") < names.index("add_flags") < names.index("uid_expunge")
    assert ("add_flags", ((1,), (b"\\Deleted",))) in client.calls
    assert ("uid_expunge", ((1,),)) in client.calls  # exactly the targeted UID
    assert sorted(client.folders["INBOX"]) == [2, 3]  # the untargeted \Deleted message survives
    assert sorted(client.folders["Trash"]) == [1]


def test_failed_uid_expunge_clears_deleted_and_reports_a_possible_duplicate():
    with mailbox(NO_MOVE) as mb:
        client = FakeIMAPClient.instances[-1]
        client.flags[("INBOX", 2)] = {b"\\Deleted"}  # untargeted, already \Deleted

        def boom(uids):
            raise OSError("connection reset")

        client.uid_expunge = boom
        with pytest.raises(MailboxError, match="copied .* not .* removed") as info:
            mb.move("INBOX", [1], "Trash")
    assert "possible duplicate" in str(info.value)
    assert ("remove_flags", ((1,), (b"\\Deleted",))) in client.calls  # only the targeted UID
    assert b"\\Deleted" not in client.flags[("INBOX", 1)]
    assert b"\\Deleted" in client.flags[("INBOX", 2)]  # others untouched
    assert "expunge" not in client.names()
    assert sorted(client.folders["INBOX"]) == [1, 2, 3] and sorted(client.folders["Trash"]) == [1]


def test_refuses_without_move_and_uidplus():
    with mailbox(NEITHER) as mb:
        client = FakeIMAPClient.instances[-1]
        with pytest.raises(MailboxError, match="neither MOVE nor UIDPLUS"):
            mb.move("INBOX", [1], "Trash")
    assert not {"move", "copy", "add_flags", "uid_expunge", "expunge"} & set(client.names())
    assert sorted(client.folders["INBOX"]) == [1, 2, 3]


def test_destination_must_exist_and_differ():
    with mailbox(FULL) as mb:
        client = FakeIMAPClient.instances[-1]
        with pytest.raises(MailboxError, match="does not exist"):
            mb.move("INBOX", [1], "Nope")
        with pytest.raises(MailboxError, match="same"):
            mb.move("INBOX", [1], "INBOX")
    assert "move" not in client.names()


@pytest.mark.parametrize("caps", [FULL, NO_MOVE, NEITHER])
def test_no_plain_expunge_or_close_in_any_path(caps):
    """Exercise every mutating operation; the fake raises on EXPUNGE-without-uids and CLOSE."""
    with mailbox(caps) as mb:
        client = FakeIMAPClient.instances[-1]
        mb.set_flags("INBOX", [1, 2], seen=True, flagged=False)
        mb.search("INBOX", SearchCriteria(unseen=True))
        mb.fetch("INBOX", 1, mark_seen=True)
        mb.append("Archive", b"Subject: x\r\n\r\nbody", ("\\Draft",))
        mb.ensure_special_folder("drafts")
        for uid, dest in ((2, "Trash"), (3, "Archive")):
            try:
                mb.move("INBOX", [uid], dest)
            except MailboxError:
                assert caps == NEITHER
    assert "expunge" not in client.names() and "close_folder" not in client.names()
    for name, args in client.calls:
        if name == "uid_expunge":
            assert args[0], "UID EXPUNGE must name its UIDs"


def test_empty_uid_lists_do_nothing():
    with mailbox(FULL) as mb:
        client = FakeIMAPClient.instances[-1]
        mb.move("INBOX", [], "Trash")
        mb.set_flags("INBOX", [], seen=True)
    assert not {"move", "copy", "add_flags", "select_folder"} & set(client.names())


def test_set_flags_adds_and_removes():
    with mailbox(FULL) as mb:
        client = FakeIMAPClient.instances[-1]
        mb.set_flags("INBOX", [1], seen=True, flagged=False)
    assert ("add_flags", ((1,), (b"\\Seen",))) in client.calls
    assert ("remove_flags", ((1,), (b"\\Flagged",))) in client.calls
    assert ("select_folder", ("INBOX", False)) in client.calls
