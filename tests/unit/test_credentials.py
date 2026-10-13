import json
import logging

import keyring
import pytest
from keyring.backend import KeyringBackend

from posta_mcp import credentials
from posta_mcp.config import load_config
from posta_mcp.credentials import CredentialsError, get_password, set_password
from posta_mcp.logs import configure_logging, redact
from posta_mcp.server import build_server

SECRET = "s3cr3t-Pw-unit-test"


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    monkeypatch.delenv("POSTA_PASSWORD", raising=False)
    return backend


def test_env_wins_over_keyring(monkeypatch, memory_keyring):
    memory_keyring.set_password(credentials.SERVICE, "u", "from-keyring")
    monkeypatch.setenv("POSTA_PASSWORD", "from-env")
    assert get_password("u") == "from-env"


def test_keyring_used_when_no_env():
    set_password("u", "from-keyring")
    assert get_password("u") == "from-keyring"


def test_missing_password_error_has_no_secret(monkeypatch):
    with pytest.raises(CredentialsError) as exc:
        get_password("u")
    assert "setup" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_secret_never_in_logs_or_server_info(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("POSTA_PASSWORD", SECRET)
    monkeypatch.setenv("POSTA_LOG_LEVEL", "DEBUG")
    log = configure_logging()
    try:
        assert get_password("u") == SECRET
        log.info("login with %s failed", SECRET)  # even a careless log call is scrubbed
        try:
            raise RuntimeError(f"boom {SECRET}")
        except RuntimeError:
            log.exception("trace %s", SECRET)

        env = {"POSTA_EMAIL": "u@example.org", "POSTA_IMAP_HOST": "i", "POSTA_SMTP_HOST": "s"}
        info = _call_server_info(load_config(env=env, path=tmp_path / "none.toml"))
        assert SECRET not in json.dumps(info)
    finally:
        logging.getLogger("posta_mcp").handlers.clear()
        logging.getLogger().handlers.clear()
    captured = capsys.readouterr()
    assert SECRET not in captured.err
    assert "***" in captured.err
    assert captured.out == ""


def _call_server_info(config):
    import anyio

    server = build_server(config)

    async def go():
        result = await server.call_tool("server_info", {})
        return result

    result = anyio.run(go)
    return result if isinstance(result, (dict, list)) else str(result)


def test_redact_helper():
    assert redact("a SECRETVALUE b", "SECRETVALUE") == "a *** b"
