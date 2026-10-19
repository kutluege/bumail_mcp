"""Passwords live in the OS keychain or the POSTA_PASSWORD env var, nowhere else."""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from posta_mcp.logs import register_secret

SERVICE = "posta-mcp"


class CredentialsError(RuntimeError):
    """Raised with a message that never contains a secret.

    `kind` is ``"no_password"`` or ``"keychain"`` (see ``posta_mcp.hints``).
    """

    def __init__(self, message: str, *, kind: str = "no_password") -> None:
        super().__init__(message)
        self.kind = kind


def get_password(username: str) -> str:
    password = os.environ.get("POSTA_PASSWORD")
    if not password:
        try:
            password = keyring.get_password(SERVICE, username)
        except KeyringError as exc:
            raise CredentialsError(
                f"Keychain unavailable ({type(exc).__name__}).", kind="keychain"
            ) from None
    if not password:
        raise CredentialsError(
            f"No password for '{username}'. Set it in the extension settings (Claude Desktop), "
            "run `posta-mcp setup` (Claude Code), or set POSTA_PASSWORD."
        )
    register_secret(password)
    return password


def has_stored_password(username: str) -> bool:
    """True if the keychain (not the env) holds a password for `username`."""
    try:
        return bool(keyring.get_password(SERVICE, username))
    except KeyringError:
        return False


def set_password(username: str, password: str) -> None:
    register_secret(password)
    try:
        keyring.set_password(SERVICE, username, password)
    except KeyringError as exc:
        raise CredentialsError(
            f"Could not store password ({type(exc).__name__}).", kind="keychain"
        ) from None


def delete_password(username: str) -> bool:
    """Remove the keychain entry for `username`. False if there was none (not an error)."""
    try:
        keyring.delete_password(SERVICE, username)
    except PasswordDeleteError:
        return False
    except KeyringError as exc:
        raise CredentialsError(
            f"Parola anahtar zincirinden silinemedi ({type(exc).__name__}).", kind="keychain"
        ) from None
    return True
