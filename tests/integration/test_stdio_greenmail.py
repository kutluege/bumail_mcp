"""Non-negotiables #1, #2, #7 end to end: the real server process reads real mail over IMAP.

Spawns ``python -m posta_mcp`` against GreenMail with ``POSTA_LOG_LEVEL=DEBUG`` and calls
``search_messages`` and ``get_message``.  stdout must carry JSON-RPC only; stderr (where the
debug log goes) must contain neither the mail's subject or body nor the password.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from email.message import EmailMessage

SUBJECT = "ZZ-Konu-Gizli-7431 Vize ğüşıöç"
BODY = "ZZ-Govde-Gizli-9982 sinav salonu degisti."


def _rpc(id_: int | None, method: str, params: dict | None = None) -> bytes:
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        message["id"] = id_
    return json.dumps(message).encode() + b"\n"


def test_imap_tools_over_stdio_keep_stdout_clean_and_stderr_free_of_mail(
    greenmail, posta_env, seed_message, tmp_path
):
    msg = EmailMessage()
    msg["From"] = "Hoca <hoca@bogazici.test>"
    msg["To"] = greenmail.student
    msg["Subject"] = SUBJECT
    msg["Date"] = "Mon, 05 Jan 2026 10:00:00 +0000"
    msg["Message-ID"] = "<stdio-test@bogazici.test>"
    msg.set_content(BODY)
    uid = seed_message(greenmail.student, msg.as_bytes())

    password = greenmail.password(greenmail.student)
    env = {
        **os.environ,
        **posta_env(),
        "POSTA_CONFIG": str(tmp_path / "absent.toml"),
        "POSTA_PROFILE": str(tmp_path / "absent-profile.toml"),
        "POSTA_LOG_LEVEL": "DEBUG",
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("POSTA_ALLOW_SEND", None)
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
    seen: list[dict] = []

    def send(data: bytes) -> None:
        proc.stdin.write(data)
        proc.stdin.flush()

    def response(id_: int) -> dict:
        while True:
            message = json.loads(lines.get(timeout=60))  # any non-JSON line fails right here
            seen.append(message)
            if message.get("id") == id_:
                return message

    def call(id_: int, name: str, arguments: dict) -> str:
        send(_rpc(id_, "tools/call", {"name": name, "arguments": arguments}))
        result = response(id_)["result"]
        assert not result.get("isError"), result
        return "\n".join(c.get("text", "") for c in result["content"])

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
        response(1)
        send(_rpc(None, "notifications/initialized"))

        found = call(2, "search_messages", {"folder": "INBOX"})
        assert SUBJECT in found and f"uid={uid}" in found  # the tool does return the mail...
        read = call(3, "get_message", {"folder": "INBOX", "uid": uid})
        assert BODY in read and "<untrusted-email" in read
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    while not lines.empty():
        seen.append(json.loads(lines.get()))
    assert all(m.get("jsonrpc") == "2.0" for m in seen)

    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    assert stderr, "DEBUG logging should have written something to stderr"
    for secret in (SUBJECT, "ZZ-Konu-Gizli-7431", BODY, "ZZ-Govde-Gizli-9982", password):
        assert secret not in stderr  # ...but nothing from the mail or the credentials is logged
