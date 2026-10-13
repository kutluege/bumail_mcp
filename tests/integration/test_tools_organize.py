"""Organize tools through the real MCP server against GreenMail."""

from __future__ import annotations

import imapclient
import pytest
from mcp_call import eml

from posta_mcp.imap import Mailbox

pytest_plugins = ["mcp_call"]  # provides the `tool` fixture

SEEN = b"\\Seen"
FLAGGED = b"\\Flagged"
DELETED = b"\\Deleted"


@pytest.fixture(autouse=True)
def no_plain_expunge(monkeypatch):
    """Product code must never issue a plain EXPUNGE (non-negotiable 5)."""
    real = imapclient.IMAPClient.expunge

    def guarded(self, messages=None):
        assert messages, "plain EXPUNGE issued by product code"
        return real(self, messages)

    monkeypatch.setattr(imapclient.IMAPClient, "expunge", guarded)


def folder_uids(imap_client, user, folder):
    c = imap_client(user)
    if not c.folder_exists(folder):
        return {}
    c.select_folder(folder, readonly=True)
    return c.get_flags(c.search("ALL"))


def test_mark_messages(tool, seed_message, greenmail, imap_client):
    u1 = seed_message(greenmail.student, eml("from_faculty.eml"))
    u2 = seed_message(greenmail.student, eml("from_student.eml"))
    out = tool("mark_messages", folder="INBOX", uids=[u1, u2], seen=True, flagged=True)
    assert not out.is_error, out.text
    flags = folder_uids(imap_client, greenmail.student, "INBOX")
    assert SEEN in flags[u1] and FLAGGED in flags[u1] and SEEN in flags[u2]
    tool("mark_messages", folder="INBOX", uids=[u1], seen=False, flagged=False)
    flags = folder_uids(imap_client, greenmail.student, "INBOX")
    assert SEEN not in flags[u1] and FLAGGED not in flags[u1]
    assert SEEN in flags[u2]  # untouched


def test_mark_messages_validates_input(tool):
    assert tool("mark_messages", folder="INBOX", uids=[1]).is_error  # nothing to set
    assert tool("mark_messages", folder="INBOX", uids=[], seen=True).is_error
    assert tool("mark_messages", folder="INBOX", uids=[0], seen=True).is_error


def test_move_messages(tool, seed_message, greenmail, imap_client):
    u1 = seed_message(greenmail.student, eml("from_faculty.eml"))
    u2 = seed_message(greenmail.student, eml("from_student.eml"))
    seed_message(greenmail.student, eml("mailing_list.eml"), folder="Okul")
    out = tool("move_messages", folder="INBOX", uids=[u1], destination="Okul")
    assert not out.is_error, out.text
    assert list(folder_uids(imap_client, greenmail.student, "INBOX")) == [u2]
    assert len(folder_uids(imap_client, greenmail.student, "Okul")) == 2

    missing = tool("move_messages", folder="INBOX", uids=[u2], destination="YokBoyleKlasor")
    assert missing.is_error and "does not exist" in missing.text
    assert list(folder_uids(imap_client, greenmail.student, "INBOX")) == [u2]


def test_trash_creates_trash_and_moves(tool, seed_message, greenmail, imap_client):
    u1 = seed_message(greenmail.student, eml("from_faculty.eml"))
    u2 = seed_message(greenmail.student, eml("from_student.eml"))
    assert not imap_client(greenmail.student).folder_exists("Trash")
    out = tool("trash_messages", folder="INBOX", uids=[u1])
    assert not out.is_error, out.text
    assert list(folder_uids(imap_client, greenmail.student, "INBOX")) == [u2]
    trash = folder_uids(imap_client, greenmail.student, "Trash")
    assert len(trash) == 1  # recoverable, not deleted
    again = tool("trash_messages", folder="Trash", uids=list(trash))
    assert again.is_error and "already in Trash" in again.text
    assert len(folder_uids(imap_client, greenmail.student, "Trash")) == 1


def test_archive_creates_archive_and_moves(tool, seed_message, greenmail, imap_client):
    u1 = seed_message(greenmail.student, eml("from_faculty.eml"))
    out = tool("archive_messages", folder="INBOX", uids=[u1])
    assert not out.is_error, out.text
    assert folder_uids(imap_client, greenmail.student, "INBOX") == {}
    assert len(folder_uids(imap_client, greenmail.student, "Archive")) == 1


@pytest.mark.parametrize("force_fallback", [False, True], ids=["uid-move", "copy-uid-expunge"])
def test_untargeted_deleted_message_survives(
    tool, seed_message, greenmail, imap_client, monkeypatch, force_fallback
):
    """A \\Deleted message the user did not target must survive trash/move/archive."""
    if force_fallback:  # pretend the server has no MOVE: exercises COPY + UID EXPUNGE on GreenMail
        real_has = Mailbox._has
        monkeypatch.setattr(
            Mailbox, "_has", lambda self, cap: False if cap == "MOVE" else real_has(self, cap)
        )
    target = seed_message(greenmail.student, eml("from_faculty.eml"))
    bystander = seed_message(greenmail.student, eml("from_student.eml"), flags=(DELETED,))
    other = seed_message(greenmail.student, eml("mailing_list.eml"))

    assert not tool("trash_messages", folder="INBOX", uids=[target]).is_error
    assert not tool("archive_messages", folder="INBOX", uids=[other]).is_error

    inbox = folder_uids(imap_client, greenmail.student, "INBOX")
    assert list(inbox) == [bystander] and DELETED in inbox[bystander]
    assert len(folder_uids(imap_client, greenmail.student, "Trash")) == 1
    assert len(folder_uids(imap_client, greenmail.student, "Archive")) == 1
    # the moved copies must not be left flagged \Deleted in the destination
    for dest in ("Trash", "Archive"):
        assert all(
            DELETED not in f for f in folder_uids(imap_client, greenmail.student, dest).values()
        )
