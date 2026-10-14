"""Local files attached to outgoing mail: path rules, typing, limits."""

from __future__ import annotations

import stat
import sys
from email import message_from_bytes, policy

import pytest

from posta_mcp import attachments
from posta_mcp.compose import build_message, send_bytes

ME = "me@example.org"

pytestmark = pytest.mark.usefixtures("no_windows_attributes")


def test_loads_and_guesses_the_type(tmp_path):
    photo = tmp_path / "çiçek.jpg"
    photo.write_bytes(b"\xff\xd8\xff fake jpeg")
    (a,) = attachments.load([str(photo)])
    assert (a.filename, a.maintype, a.subtype) == ("çiçek.jpg", "image", "jpeg")
    assert a.data == b"\xff\xd8\xff fake jpeg"
    assert a.path == str(photo.resolve())


def test_unknown_or_compressed_types_go_as_octet_stream(tmp_path):
    for name in ("veri.bilinmeyen", "arsiv.tar.gz"):
        (tmp_path / name).write_bytes(b"x")
    types = {
        (a.maintype, a.subtype) for a in attachments.load([str(p) for p in tmp_path.iterdir()])
    }
    assert types == {("application", "octet-stream")}


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # what Windows expands ~ with
    (tmp_path / "not.txt").write_text("merhaba")
    (a,) = attachments.load(["~/not.txt"])
    assert a.filename == "not.txt"


@pytest.mark.parametrize("raw", ["relative.txt", "", "  ", "/tmp/a\nb"])
def test_relative_empty_or_control_character_paths_are_refused(raw):
    with pytest.raises(ValueError):
        attachments.load([raw])


def test_missing_file_and_directory_are_refused(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        attachments.load([str(tmp_path / "yok.pdf")])
    with pytest.raises(ValueError, match="not a regular file"):
        attachments.load([str(tmp_path)])


@pytest.mark.parametrize("rel", [".env", ".ssh/id_ed25519", "proje/.git/config"])
def test_hidden_files_and_folders_are_refused(tmp_path, rel):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret")
    with pytest.raises(ValueError, match="hidden"):
        attachments.load([str(target)])


def test_symlink_to_a_hidden_file_is_refused(tmp_path):
    secret = tmp_path / ".aws" / "credentials"
    secret.parent.mkdir()
    secret.write_text("secret")
    link = tmp_path / "rapor.pdf"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
    with pytest.raises(ValueError, match="hidden"):
        attachments.load([str(link)])


@pytest.mark.parametrize(
    "raw", [r"\\sunucu\paylasim\rapor.pdf", "//sunucu/paylasim/rapor.pdf", r"\\?\C:\rapor.pdf"]
)
def test_network_paths_are_refused_without_touching_them(raw, monkeypatch):
    def touched(*args, **kwargs):
        raise AssertionError("the file system was accessed")

    monkeypatch.setattr(attachments.Path, "resolve", touched)
    with pytest.raises(ValueError, match="network paths"):
        attachments.load([raw])


def test_folder_with_the_windows_hidden_attribute_is_refused(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    target = appdata / "Local" / "Tarayici" / "Cookies"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"secret")
    monkeypatch.setattr(
        attachments, "_attributes", lambda p: stat.FILE_ATTRIBUTE_HIDDEN if p == appdata else 0
    )
    with pytest.raises(ValueError, match="hidden"):
        attachments.load([str(target)])
    (tmp_path / "rapor.pdf").write_bytes(b"x")
    assert attachments.load([str(tmp_path / "rapor.pdf")])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_real_appdata_is_refused_on_windows(tmp_path, monkeypatch):
    monkeypatch.undo()  # the real attributes: tmp_path is under the hidden AppData folder
    if "AppData" not in tmp_path.parts:
        pytest.skip("the temp folder is not under AppData here")
    (tmp_path / "rapor.pdf").write_bytes(b"x")
    with pytest.raises(ValueError, match="hidden"):
        attachments.load([str(tmp_path / "rapor.pdf")])


def test_limits(tmp_path, monkeypatch):
    files = []
    for i in range(attachments.MAX_FILES + 1):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        files.append(str(f))
    with pytest.raises(ValueError, match="at most"):
        attachments.load(files)
    with pytest.raises(ValueError, match="more than once"):
        attachments.load([files[0], files[0]])
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 10)
    (tmp_path / "big.bin").write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="limit"):
        attachments.load([str(tmp_path / "big.bin")])


def test_build_message_adds_attachments_after_the_text(tmp_path):
    pdf = tmp_path / "ödev raporu.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    note = tmp_path / "not.txt"
    note.write_text("düz metin ek")
    msg = build_message(
        ME,
        ["a@example.org"],
        "Ödev",
        "Ekte.\n",
        attachments=attachments.load([str(pdf), str(note)]),
    )
    parsed = message_from_bytes(send_bytes(msg), policy=policy.default)
    assert parsed.get_body(("plain",)).get_content().strip() == "Ekte."
    got = {
        p.get_filename(): (p.get_content_type(), p.get_content()) for p in parsed.iter_attachments()
    }
    assert got["ödev raporu.pdf"] == ("application/pdf", b"%PDF-1.4 fake")
    assert got["not.txt"][0] == "text/plain"


def test_message_with_attachments_serialises_to_the_same_bytes_every_time(tmp_path):
    pdf = tmp_path / "rapor.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    msg = build_message(ME, ["a@example.org"], "x", "x", attachments=attachments.load([str(pdf)]))
    assert send_bytes(msg) == send_bytes(msg)
