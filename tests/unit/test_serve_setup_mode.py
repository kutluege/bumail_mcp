"""`posta-mcp serve` without usable settings starts a minimal "kurulum gerekli" server."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading

import pytest

from posta_mcp.cli import main

_ENV_NAMES = ("POSTA_EMAIL", "POSTA_PRESET", "POSTA_IMAP_HOST", "POSTA_SMTP_HOST", "POSTA_USERNAME")


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Record what cmd_serve would serve instead of running stdio."""
    monkeypatch.setenv("POSTA_CONFIG", str(tmp_path / "none.toml"))
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    calls: dict[str, list] = {"setup": [], "full": []}
    monkeypatch.setattr("posta_mcp.server.run_setup_required", calls["setup"].append)
    monkeypatch.setattr("posta_mcp.server.run", calls["full"].append)
    return calls


def test_missing_settings_serve_setup_help(served, capsys):
    assert main(["serve"]) == 0
    assert served["full"] == [] and len(served["setup"]) == 1
    assert "e-posta adresi" in served["setup"][0]
    captured = capsys.readouterr()
    assert "setup required" in captured.err and "missing settings" in captured.err
    assert captured.out == ""  # stdout belongs to the MCP channel


def test_invalid_config_serves_setup_help(served, monkeypatch, capsys):
    monkeypatch.setenv("POSTA_PRESET", "bogazici")
    monkeypatch.setenv("POSTA_EMAIL", "someone@gmail.com")  # matches no Boğaziçi account type
    assert main(["serve"]) == 0
    assert served["full"] == []
    problem = served["setup"][0]
    assert "Ayarlarda bir sorun var" in problem and "std.bogazici.edu.tr" in problem
    assert capsys.readouterr().out == ""


def test_complete_settings_serve_the_full_server(served, monkeypatch):
    monkeypatch.setenv("POSTA_PRESET", "bogazici")
    monkeypatch.setenv("POSTA_EMAIL", "ada.lovelace@std.bogazici.edu.tr")
    assert main(["serve"]) == 0
    assert served["setup"] == [] and served["full"][0].email == "ada.lovelace@std.bogazici.edu.tr"


def _rpc(id_: int, method: str, params: dict | None = None) -> bytes:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return json.dumps(msg).encode() + b"\n"


def test_unconfigured_server_starts_over_stdio_and_explains_setup(tmp_path):
    env = {
        **os.environ,
        "POSTA_CONFIG": str(tmp_path / "none.toml"),
        "PYTHONIOENCODING": "utf-8",
    }
    for name in (*_ENV_NAMES, "POSTA_PASSWORD"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "posta_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    lines: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()
    threading.Thread(target=proc.stderr.read, daemon=True).start()

    def call(id_: int, method: str, params: dict | None = None) -> dict:
        proc.stdin.write(_rpc(id_, method, params))
        proc.stdin.flush()
        while True:
            message = json.loads(lines.get(timeout=30))
            assert message.get("jsonrpc") == "2.0"
            if message.get("id") == id_:
                return message

    try:
        init = call(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "posta-mcp"
        proc.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        tools = [t["name"] for t in call(2, "tools/list")["result"]["tools"]]
        assert tools == ["server_info"]
        info = call(3, "tools/call", {"name": "server_info", "arguments": {}})["result"]
        data = info["structuredContent"]
        assert data["setup_required"] is True and "e-posta adresi" in data["problem"]
        assert "uvx posta-mcp setup" in data["what_to_do"]
        prompts = [p["name"] for p in call(4, "prompts/list")["result"]["prompts"]]
        assert prompts == ["baslangic"]
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
