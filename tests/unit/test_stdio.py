"""Non-negotiable #1: stdout carries only JSON-RPC. Also: no secret in any output."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading

FAKE_PASSWORD = "fake-pw-Zx9!unit-test"

CONFIG = """\
preset = "generic"
email = "student@example.org"

[imap]
host = "imap.example.org"

[smtp]
host = "smtp.example.org"
"""


def _rpc(id_: int, method: str, params: dict | None = None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def test_stdout_is_only_jsonrpc_and_server_info_has_no_secret(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG, encoding="utf-8")
    env = {
        **os.environ,
        "POSTA_CONFIG": str(config),
        "POSTA_PASSWORD": FAKE_PASSWORD,
        "POSTA_LOG_LEVEL": "DEBUG",
        "PYTHONIOENCODING": "utf-8",
    }
    for name in ("POSTA_PRESET", "POSTA_EMAIL", "POSTA_ALLOW_SEND"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "posta_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    lines: queue.Queue[bytes] = queue.Queue()
    stderr_chunks: list[bytes] = []
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: stderr_chunks.extend(proc.stderr), daemon=True).start()

    def send(text: str) -> None:
        proc.stdin.write(text.encode() + b"\n")
        proc.stdin.flush()

    def response(id_: int) -> dict:
        while True:
            message = json.loads(lines.get(timeout=30))
            seen.append(message)
            if message.get("id") == id_:
                return message

    seen: list[dict] = []
    raw_out: list[bytes] = []
    try:
        send(
            _rpc(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            )
        )
        init = response(1)
        assert init["result"]["serverInfo"]["name"] == "posta-mcp"
        send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))

        send(_rpc(2, "tools/list"))
        tools = {t["name"]: t for t in response(2)["result"]["tools"]}
        assert tools["server_info"]["annotations"]["readOnlyHint"] is True

        send(_rpc(3, "tools/call", {"name": "server_info", "arguments": {}}))
        info = response(3)
        text = json.dumps(info)
        assert "imap.example.org" in text
        assert FAKE_PASSWORD not in text
        assert '"sending_enabled": false' in json.dumps(info["result"]["structuredContent"])
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        # drain whatever else reached stdout: every line must still be JSON-RPC
        while not lines.empty():
            raw_out.append(lines.get())
    for raw in raw_out:
        seen.append(json.loads(raw))
    assert seen, "server produced no output"
    assert all(m.get("jsonrpc") == "2.0" for m in seen)
    assert FAKE_PASSWORD.encode() not in b"".join(stderr_chunks)
