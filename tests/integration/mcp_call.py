"""Call posta-mcp tools through a real in-memory MCP client against GreenMail.

Usage in a test module::

    pytest_plugins = ["mcp_call"]  # then use the `tool` fixture

    def test_x(tool):
        out = tool("search_messages", folder="INBOX")
        assert not out.is_error and "untrusted-email" in out.text
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp import Client

from posta_mcp.config import load_config
from posta_mcp.server import build_server

EML = Path(__file__).resolve().parents[1] / "fixtures" / "eml"


def eml(name: str) -> bytes:
    """Raw bytes of a Turkish test fixture, with CRLF line endings for IMAP APPEND."""
    data = (EML / name).read_bytes()
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


@dataclass
class ToolOutput:
    text: str
    is_error: bool


@pytest.fixture
def tool(posta_env, monkeypatch) -> Callable[..., ToolOutput]:
    """``tool(name, **args)`` -> ToolOutput. ``tool.use(user, **env)`` switches account."""
    state = {"env": posta_env()}

    def apply(env: dict[str, str]) -> None:
        state["env"] = env
        monkeypatch.setenv("POSTA_PASSWORD", env["POSTA_PASSWORD"])

    apply(state["env"])

    async def _call(name: str, args: dict) -> ToolOutput:
        server = build_server(load_config(env=state["env"]))
        async with Client(server) as client:
            result = await client.call_tool(name, args)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        return ToolOutput(text, bool(result.is_error))

    def call(name: str, **args) -> ToolOutput:
        return asyncio.run(_call(name, args))

    call.use = lambda user=None, **env: apply(posta_env(user, **env))  # type: ignore[attr-defined]
    return call
