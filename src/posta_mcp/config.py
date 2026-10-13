"""Configuration: preset TOML -> user config.toml -> environment overrides. Non-secret only."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, ValidationError

APP_NAME = "posta-mcp"
MAX_MESSAGE_BYTES = 25 * 1024 * 1024  # larger messages are never fetched in full (memory guard)
Security = Literal["ssl", "starttls"]  # plaintext is intentionally not representable


class ConfigError(ValueError):
    """Bad configuration. Messages never include values, only field names."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Section(_Model):
    verified: bool = True  # presets set false until checked against the live server


class Endpoint(_Section):
    host: str = ""
    port: int = Field(gt=0, lt=65536)
    security: Security = "ssl"


class Folders(_Section):
    sent: str = "Sent"
    drafts: str = "Drafts"
    trash: str = "Trash"
    archive: str = "Archive"


class Domains(_Section):
    institution: list[str] = []
    student: list[str] = []
    alumni: list[str] = []  # never "faculty", even when under an institution domain


class Classification(_Section):
    list_addresses: list[str] = []
    list_domains: list[str] = []
    standard_addresses: list[str] = []
    standard_local_parts: list[str] = []


class Sending(_Model):
    enabled: bool = False


class Config(_Model):
    preset: str = "generic"
    variant: str = ""  # which of the preset's account types matched the email domain
    username_style: Literal["email", "localpart"] = "email"
    email: str = ""
    username: str = ""
    imap: Endpoint
    smtp: Endpoint
    folders: Folders = Folders()
    domains: Domains = Domains()
    classification: Classification = Classification()
    sending: Sending = Sending()
    save_sent: Literal["auto", "always", "never"] = "auto"
    ca_file: str | None = None  # extra trusted CA (tests); never disables verification

    @property
    def login(self) -> str:
        """IMAP/SMTP login: explicit username, else per the preset's username_style."""
        if self.username:
            return self.username
        if self.username_style == "localpart":
            return self.email.partition("@")[0]
        return self.email

    @property
    def send_enabled(self) -> bool:
        return self.sending.enabled

    @property
    def unverified(self) -> list[str]:
        """Preset fields not yet confirmed against the live server, as 'section.field'."""
        out: list[str] = []
        for section in ("imap", "smtp", "folders", "domains", "classification"):
            model: _Section = getattr(self, section)
            if not model.verified:
                out += [f"{section}.{f}" for f in type(model).model_fields if f != "verified"]
        return out

    def missing(self) -> list[str]:
        """Required settings that are still empty (checked before serving)."""
        need = {"email": self.email, "imap.host": self.imap.host, "smtp.host": self.smtp.host}
        return [name for name, value in need.items() if not value]


def _get(env: Mapping[str, str], name: str) -> str | None:
    """An env value; empty or whitespace-only counts as unset (MCP hosts pass blanks)."""
    value = env.get(name, "").strip()
    return value or None


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if override := _get(env, "POSTA_CONFIG"):
        return Path(override)
    return Path(platformdirs.user_config_dir(APP_NAME)) / "config.toml"


def available_presets() -> list[str]:
    root = resources.files("posta_mcp") / "presets"
    return sorted(p.name.removesuffix(".toml") for p in root.iterdir() if p.name.endswith(".toml"))


def _load_preset(name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_-]+", name) or name not in available_presets():
        raise ConfigError(
            f"Unknown preset '{name[:40]}'. Available: {', '.join(available_presets())}."
        )
    text = (resources.files("posta_mcp") / "presets" / f"{name}.toml").read_text("utf-8")
    data = tomllib.loads(text)
    data.pop("name", None)
    data["preset"] = name
    return data


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(env: Mapping[str, str], name: str) -> bool | None:
    value = _get(env, name)
    if value is None:
        return None
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ConfigError(f"{name} must be one of 1/true/yes/on or 0/false/no/off.")


def _read_user_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"Cannot read {path.name}: {type(exc).__name__}.") from None
    if any("password" in key.lower() for key in data):
        raise ConfigError("Passwords must not be in the config file; use `posta-mcp setup`.")
    return data


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("email", "username"):
        if value := _get(env, f"POSTA_{key.upper()}"):
            out[key] = value
    if value := _get(env, "POSTA_CA_FILE"):
        out["ca_file"] = value
    for proto in ("imap", "smtp"):
        section: dict[str, Any] = {}
        for field in ("host", "port", "security"):
            if value := _get(env, f"POSTA_{proto.upper()}_{field.upper()}"):
                section[field] = value.lower() if field == "security" else value
        if "port" in section:
            if not section["port"].isdigit():
                raise ConfigError(f"POSTA_{proto.upper()}_PORT must be a number.")
            section["port"] = int(section["port"])
        if section:
            out[proto] = section
    if (allow := _env_bool(env, "POSTA_ALLOW_SEND")) is not None:
        out["sending"] = {"enabled": allow}
    return out


def _pick_variant(variants: list[dict[str, Any]], email: str) -> dict[str, Any] | None:
    domain = email.rpartition("@")[2].lower()
    return next((v for v in variants if domain in v["domains"]), None)


def _build(user: dict[str, Any], env: Mapping[str, str]) -> Config:
    preset = _get(env, "POSTA_PRESET") or user.get("preset") or "generic"
    overrides = _env_overrides(env)
    email = overrides.get("email") or user.get("email") or ""

    data = _load_preset(preset)
    variants = data.pop("variants", [])
    variant = _pick_variant(variants, email)
    if variant:  # account-type specific hosts: preset base < variant < user file < env
        _merge(data, {k: variant[k] for k in ("imap", "smtp")})
    _merge(data, user)
    _merge(data, overrides)
    data["preset"] = preset
    data["variant"] = variant["name"] if variant else ""

    has_hosts = data["imap"].get("host") and data["smtp"].get("host")
    if variants and email and not variant and not has_hosts:
        expected = ", ".join(d for v in variants for d in v["domains"])
        raise ConfigError(
            f"Email domain '{email.rpartition('@')[2][:60]}' matches no '{preset}' account type. "
            f"Expected an address at: {expected} (or set imap.host and smtp.host yourself)."
        )
    try:
        return Config.model_validate(data)
    except ValidationError as exc:  # report locations/messages only, never input values
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"Invalid configuration: {problems}") from None


def load_config(env: Mapping[str, str] | None = None, path: Path | None = None) -> Config:
    env = os.environ if env is None else env
    return _build(_read_user_file(path or config_path(env)), env)


def preview_config(preset: str, email: str) -> Config:
    """What a preset + email would resolve to, ignoring any existing config file or env."""
    return _build({"preset": preset, "email": email}, {})
