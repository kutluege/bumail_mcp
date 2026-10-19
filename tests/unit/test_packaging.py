"""Packaging metadata (wheel, MCPB manifest, MCP Registry server.json, README) and the pure
helpers of scripts/live_verify.py. No network except that building a wheel may fetch hatchling."""

from __future__ import annotations

import asyncio
import fnmatch
import importlib.util
import json
import re
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile
from importlib import resources
from pathlib import Path

import pytest

from posta_mcp import __version__

ROOT = Path(__file__).resolve().parents[2]


def load_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ presets and the wheel


@pytest.mark.parametrize("name", ["bogazici", "generic"])
def test_presets_are_package_data(name):
    text = (resources.files("posta_mcp") / "presets" / f"{name}.toml").read_text("utf-8")
    assert tomllib.loads(text)["name"] == name


def test_wheel_target_covers_the_package_directory():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/posta_mcp"]
    assert pyproject["project"]["version"] == __version__
    assert pyproject["project"]["scripts"]["posta-mcp"] == "posta_mcp.cli:main"
    assert pyproject["project"]["license"] == "MIT"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_built_wheel_contains_presets_and_license(tmp_path):
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-800:]
    (wheel,) = tmp_path.glob("posta_mcp-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert "posta_mcp/presets/bogazici.toml" in names
    assert "posta_mcp/presets/generic.toml" in names
    assert any(n.endswith("licenses/LICENSE") for n in names)
    assert not any(n.startswith(("tests/", "docs/", ".greenmail/")) for n in names)
    entry_points = zipfile.ZipFile(wheel).read(
        next(n for n in names if n.endswith("entry_points.txt"))
    )
    assert b"posta-mcp = posta_mcp.cli:main" in entry_points


def test_license_is_mit_with_contributors_line():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "posta-mcp contributors" in text


# ------------------------------------------------------------------ MCPB manifest


def test_manifest_basics():
    m = load_json("manifest.json")
    assert m["manifest_version"] == "0.4"
    assert m["version"] == __version__
    assert m["license"] == "MIT"
    assert m["server"]["type"] == "uv"
    assert (ROOT / m["server"]["entry_point"]).is_file()
    assert set(m["compatibility"]["platforms"]) <= {"darwin", "win32", "linux"}
    assert m["name"] == "posta-mcp"
    assert m["display_name"] == "Boğaziçi Postanesi"
    assert "unofficial" in m["long_description"].lower()
    assert "not affiliated" in m["long_description"].lower()


def test_manifest_icon_is_a_512_png_kept_in_the_bundle():
    m = load_json("manifest.json")
    icon = ROOT / m["icon"]
    data = icon.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (512, 512)
    lines = (ROOT / ".mcpbignore").read_text(encoding="utf-8").splitlines()
    ignored = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert not any(fnmatch.fnmatch(m["icon"], pattern) for pattern in ignored)


def test_manifest_user_config_maps_to_env():
    m = load_json("manifest.json")
    cfg = m["user_config"]
    # Desktop bundle is Boğaziçi-only: no preset or host fields, no ca_file for users
    assert set(cfg) == {"email", "password", "allow_send"}
    assert cfg["password"]["sensitive"] is True
    assert cfg["allow_send"]["type"] == "boolean" and cfg["allow_send"]["default"] is False
    for field in cfg.values():
        assert field["type"] and field["title"] and field["description"]
    assert "std.bogazici.edu.tr" in cfg["email"]["description"]
    assert "BUmail" in cfg["password"]["title"]
    env = m["server"]["mcp_config"]["env"]
    assert env == {
        "POSTA_PRESET": "bogazici",
        "POSTA_EMAIL": "${user_config.email}",
        "POSTA_PASSWORD": "${user_config.password}",
        "POSTA_ALLOW_SEND": "${user_config.allow_send}",
    }
    assert "CA_FILE" not in json.dumps(m)


def _registered(kind: str) -> set[str]:
    from mcp import Client

    from posta_mcp.config import load_config
    from posta_mcp.server import build_server

    env = {
        "POSTA_PRESET": "generic",
        "POSTA_EMAIL": "a@example.test",
        "POSTA_IMAP_HOST": "127.0.0.1",
        "POSTA_SMTP_HOST": "127.0.0.1",
    }
    server = build_server(load_config(env=env, path=ROOT / "no-such-config.toml"))

    async def go() -> set[str]:
        async with Client(server) as client:
            if kind == "tools":
                return {t.name for t in (await client.list_tools()).tools}
            return {p.name for p in (await client.list_prompts()).prompts}

    return asyncio.run(go())


def test_manifest_lists_every_registered_tool_and_prompt():
    m = load_json("manifest.json")
    listed_tools = {t["name"] for t in m["tools"]}
    listed_prompts = {p["name"] for p in m["prompts"]}
    assert _registered("tools") <= listed_tools
    assert _registered("prompts") <= listed_prompts
    assert {"baslangic", "hoca-ozet", "ogrenci-ozet", "bana-ozet"} == listed_prompts
    for prompt in m["prompts"]:
        assert prompt["text"]


def test_manifest_baslangic_prompt():
    m = load_json("manifest.json")
    (prompt,) = [p for p in m["prompts"] if p["name"] == "baslangic"]
    assert not prompt.get("arguments")
    text = prompt["text"]
    for needle in (
        "sunucu_bilgisi",
        "klasorleri_listele",
        "profili_goster",
        "hoca-ozet",
        "ogrenci-ozet",
        "bana-ozet",
        "Turkish",
        "untrusted data",
    ):
        assert needle in text, needle


# ------------------------------------------------------------------ server.json and README


def test_server_json_registry_shape():
    s = load_json("server.json")
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", s["name"])
    assert s["name"] == "io.github.kutluege/posta-mcp"
    assert len(s["description"]) <= 100
    assert s["version"] == __version__
    (pkg,) = s["packages"]
    assert (pkg["registryType"], pkg["identifier"], pkg["version"]) == (
        "pypi",
        "posta-mcp",
        __version__,
    )
    assert pkg["transport"] == {"type": "stdio"}
    env = {e["name"]: e for e in pkg["environmentVariables"]}
    assert env["POSTA_PASSWORD"]["isSecret"] is True
    assert not env["POSTA_PASSWORD"].get("default")
    assert env["POSTA_ALLOW_SEND"]["default"] == "false"


def test_readme_carries_registry_ownership_marker_and_turkish_disclaimers():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    name = load_json("server.json")["name"]
    assert readme.splitlines()[0] == f"<!-- mcp-name: {name} -->"
    lowered = " ".join(readme.lower().split())
    for phrase in (
        "resmi değildir",
        "onaylanmamıştır",
        "kabul edilebilir kullanım",
        "yapay zekâ sağlayıcısına",
        "yanlış özetleyebilir",
        "varsayılan olarak kapalıdır",
        "senin onayını",
        "sen sorumlusun",
        "mit",
        "hoca-ozet",
        "ogrenci-ozet",
        "bana-ozet",
        "docs/readme.en.md",
    ):
        assert phrase in lowered, phrase


def test_english_readme_carries_disclaimers():
    readme = (ROOT / "docs" / "README.en.md").read_text(encoding="utf-8")
    lowered = " ".join(readme.lower().split())  # line breaks do not matter
    for phrase in (
        "unofficial",
        "not affiliated with or endorsed by",
        "acceptable-use",
        "model provider",
        "mis-summarize",
        "off by default",
        "your confirmation",
        "responsible for what you send",
        "as-is, without warranty",
        "unverified",
        "hoca-ozet",
        "ogrenci-ozet",
        "bana-ozet",
    ):
        assert phrase in lowered, phrase


def test_docs_exist():
    for name in ("docs/CLIENTS.md", "docs/HANDOFF.md", "scripts/live_verify.py"):
        assert (ROOT / name).is_file(), name


# ------------------------------------------------------------------ live_verify helpers


@pytest.fixture(scope="module")
def lv():
    spec = importlib.util.spec_from_file_location(
        "live_verify", ROOT / "scripts" / "live_verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_verify"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


def test_live_verify_args_default_to_read_only(lv):
    args = lv.parse_args([])
    assert not (args.append_draft or args.send_self or args.show_subjects)
    args = lv.parse_args(["--append-draft", "--send-self", "--show-subjects", "--timeout", "5"])
    assert args.append_draft and args.send_self and args.show_subjects and args.timeout == 5
    with pytest.raises(SystemExit):
        lv.parse_args(["--timeout", "0"])
    with pytest.raises(SystemExit):
        lv.parse_args(["--no-such-flag"])


def test_live_verify_send_self_confirmation(lv):
    assert lv.confirm_matches(
        "  Ada.Lovelace@Std.Bogazici.edu.tr\n", "ada.lovelace@std.bogazici.edu.tr"
    )
    assert not lv.confirm_matches("", "a@b.test")
    assert not lv.confirm_matches("y", "a@b.test")
    assert not lv.confirm_matches("other@b.test", "a@b.test")


def test_live_verify_capabilities(lv):
    flags = lv.capability_flags([b"IMAP4rev1", b"move", "UIDPLUS", b"IDLE"])
    assert flags == {"MOVE": True, "UIDPLUS": True, "SPECIAL-USE": False, "IDLE": True}


def test_live_verify_compare_folders(lv):
    preset = {"sent": "Sent", "drafts": "Drafts", "trash": "Trash", "archive": "Archive"}
    folders = [
        ("INBOX", None),
        ("Sent", "sent"),
        ("Drafts", None),
        ("Deleted Items", "trash"),
    ]
    result = lv.compare_folders(folders, preset)
    assert result["sent"][0] == lv.OK and "SPECIAL-USE agrees" in result["sent"][1]
    assert result["drafts"][0] == lv.OK
    assert result["trash"][0] == lv.MISMATCH and "Deleted Items" in result["trash"][1]
    assert result["archive"][0] == lv.OK and "created on first archive" in result["archive"][1]


def test_live_verify_charset_names_never_returns_content(lv):
    raw = (
        b"From: a@b.test\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=ISO-8859-9\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n\xdd\xfeyi\r\n"
    )
    assert lv.charset_names(raw) == ["iso-8859-9"]
    assert lv.charset_names(b"Subject: x\r\n\r\nplain ascii\r\n") == []


def test_live_verify_report_formatting_and_edits(lv):
    report = lv.Report()
    for name in ("imap.host", "imap.port", "imap.security"):
        report.add(name, lv.OK, "fine")
    report.add("smtp.host", lv.MISMATCH, "refused")
    report.add("folders.sent", lv.OK, "ok")
    report.add("domains.student", lv.UNTESTED, "by hand")
    text = lv.format_report(report)
    assert "imap.host" in text and "MISMATCH" in text and "UNTESTED" in text
    assert report.status("smtp.host") == lv.MISMATCH
    assert report.status("nope") == lv.UNTESTED

    edits = "\n".join(lv.edit_instructions("bogazici", "student", report.rows))
    assert '[variants.imap] of the [[variants]] entry named "student" set  verified = true' in edits
    assert "[variants.smtp]" not in edits  # smtp mismatched
    assert "[folders] set  verified = true" in edits
    assert "[domains] and [classification] cannot be confirmed" in edits

    nothing = "\n".join(lv.edit_instructions("bogazici", "", [lv.Row("imap.host", lv.MISMATCH)]))
    assert "No preset section was fully confirmed" in nothing
    assert "nothing to mark verified" in lv.edit_instructions("generic", "", [])[0]


def test_live_verify_never_prints_the_password():
    source = (ROOT / "scripts" / "live_verify.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "print(" in line:
            assert "password" not in line.lower().replace("the password is never printed", "")
