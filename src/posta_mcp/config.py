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


class Classification(_Section):
    list_addresses: list[str] = []
    list_domains: list[str] = []
    standard_addresses: list[str] = []
    standard_local_parts: list[str] = []


class Sending(_Model):
    enabled: bool = False


class Config(_Model):
    preset: str = "generic"
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
        return self.username or self.email

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


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("POSTA_CONFIG"):
        return Path(env["POSTA_CONFIG"])
    return Path(platformdirs.user_config_dir(APP_NAME)) / "config.toml"


def available_presets() -> list[str]:
    root = resources.files("posta_mcp") / "presets"
    return sorted(p.name.removesuffix(".toml") for p in root.iterdir() if p.name.endswith(".toml"))


def _load_preset(name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_-]+", name) or name not in available_presets():
        raise ConfigError(f"Unknown preset '{name[:40]}'. Available: {', '.join(available_presets())}.")
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


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        if env.get(f"POSTA_{key.upper()}"):
            out[key] = env[f"POSTA_{key.upper()}"]
    if env.get("POSTA_CA_FILE"):
        out["ca_file"] = env["POSTA_CA_FILE"]
    for proto in ("imap", "smtp"):
        section: dict[str, Any] = {}
        for field in ("host", "port", "security"):
            value = env.get(f"POSTA_{proto.upper()}_{field.upper()}")
            if value:
                section[field] = value.strip().lower() if field == "security" else value.strip()
        if "port" in section:
            if not section["port"].isdigit():
                raise ConfigError(f"POSTA_{proto.upper()}_PORT must be a number.")
            section["port"] = int(section["port"])
        if section:
            out[proto] = section
    if "POSTA_ALLOW_SEND" in env:
        out["sending"] = {"enabled": _truthy(env["POSTA_ALLOW_SEND"])}
    return out


def load_config(env: Mapping[str, str] | None = None, path: Path | None = None) -> Config:
    env = os.environ if env is None else env
    user = _read_user_file(path or config_path(env))
    preset = env.get("POSTA_PRESET") or user.get("preset") or "generic"
    data = _merge(_load_preset(preset), user)
    _merge(data, _env_overrides(env))
    data["preset"] = preset
    try:
        return Config.model_validate(data)
    except ValidationError as exc:  # report locations/messages only, never input values
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"Invalid configuration: {problems}") from None
