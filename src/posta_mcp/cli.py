"""Command line: `posta-mcp [serve] | setup | doctor | --version`."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from posta_mcp import __version__
from posta_mcp.config import (
    Config,
    ConfigError,
    available_presets,
    config_path,
    load_config,
    preview_config,
)
from posta_mcp.credentials import CredentialsError, get_password, has_stored_password, set_password
from posta_mcp.logs import configure_logging, redact
from posta_mcp.tls import make_ssl_context


def _fail(message: str) -> int:
    print(f"posta-mcp: {message}", file=sys.stderr)
    return 2


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def _write_config(
    path: Path, preset: str, email: str, username: str, hosts: dict[str, str]
) -> None:
    lines = [f"preset = {_toml_str(preset)}", f"email = {_toml_str(email)}"]
    if username:
        lines.append(f"username = {_toml_str(username)}")
    for proto, host in hosts.items():
        lines += ["", f"[{proto}]", f"host = {_toml_str(host)}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def try_imap_login(config: Config, password: str) -> str:
    """Log in once over verified TLS. Returns exactly "OK", or a Turkish explanation of the
    failure with a short technical tail; never includes the password."""
    from imapclient import IMAPClient

    from posta_mcp.hints import with_hint

    context = make_ssl_context(config.ca_file)
    try:
        imap = config.imap
        client = IMAPClient(
            imap.host, port=imap.port, ssl=imap.security == "ssl", ssl_context=context, timeout=20
        )
        with client:
            if imap.security == "starttls":
                client.starttls(ssl_context=context)
            client.login(config.login, password)
            return "OK"
    except Exception as exc:  # noqa: BLE001 - report the failure kind, scrub anything secret-like
        technical = redact(f"{type(exc).__name__}: {exc}", password)
        return redact(with_hint(exc, technical, fallback="Giriş denemesi başarısız."), password)


def cmd_serve(_: argparse.Namespace) -> int:
    configure_logging()
    from posta_mcp.hints import setup_problem
    from posta_mcp.server import run, run_setup_required

    # Unusable settings do not stop the server: it starts in a minimal "kurulum gerekli" mode
    # (no mailbox access) whose server_info explains the fix, instead of a bare "failed".
    try:
        config = load_config()
    except ConfigError as exc:
        problem, detail = setup_problem(error=exc), str(exc)
    else:
        if not config.missing():
            run(config)
            return 0
        problem = setup_problem(missing=config.missing())
        detail = f"missing settings: {', '.join(config.missing())}"
    # English on stderr: a piped stderr on Windows may not be able to encode Turkish letters.
    print(f"posta-mcp: setup required ({detail}); serving setup help only.", file=sys.stderr)
    run_setup_required(problem)
    return 0


def cmd_setup(_: argparse.Namespace) -> int:
    path = config_path()
    if path.exists() and input(f"{path} exists. Overwrite? [y/N] ").strip().lower() != "y":
        return _fail("aborted; config unchanged.")
    presets = available_presets()
    preset = input(f"Preset ({'/'.join(presets)}) [bogazici]: ").strip() or "bogazici"
    if preset not in presets:
        return _fail(f"unknown preset '{preset[:40]}'.")
    email = input("Email address: ").strip()
    try:
        preview = preview_config(preset, email)  # picks the account type from the email domain
    except ConfigError as exc:
        return _fail(str(exc))
    default_login = preview.login
    username = input(f"Login username [{default_login}]: ").strip() or default_login
    hosts: dict[str, str] = {}
    if preset == "generic":
        hosts = {"imap": input("IMAP host: ").strip(), "smtp": input("SMTP host: ").strip()}
    else:
        imap, smtp = preview.imap, preview.smtp
        print(f"Account type: {preview.variant or preset}")
        print(f"  IMAP: {imap.host}:{imap.port} ({imap.security})")
        print(f"  SMTP: {smtp.host}:{smtp.port} ({smtp.security})")
        if preview.unverified:
            print("  (these preset values are unverified; `posta-mcp doctor` will test the login)")
    password = getpass.getpass("Password (not shown, stored in your OS keychain): ")
    if not (email and password):
        return _fail("email and password are required.")
    try:
        set_password(username, password)
    except CredentialsError as exc:
        return _fail(str(exc))
    # only record a username that differs from what the preset derives from the email
    _write_config(path, preset, email, "" if username == default_login else username, hosts)
    print(f"Saved settings to {path} (no password in it) and the password to the keychain.")
    try:
        config = load_config()
    except ConfigError as exc:
        return _fail(str(exc))
    print(f"IMAP login test: {try_imap_login(config, password)}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        return _fail(str(exc))
    print(f"posta-mcp {__version__}")
    print(f"config file : {config_path()} ({'found' if config_path().is_file() else 'not found'})")
    print(f"preset      : {config.preset}")
    print(f"account     : {config.email or '(not set)'} ({config.variant or 'no account type'})")
    print(f"login       : {config.login or '(not set)'}")
    print(f"IMAP        : {config.imap.host}:{config.imap.port} ({config.imap.security})")
    print(f"SMTP        : {config.smtp.host}:{config.smtp.port} ({config.smtp.security})")
    print(f"sending     : {'ENABLED' if config.send_enabled else 'disabled'}")
    print(f"unverified  : {', '.join(config.unverified) or 'none'}")
    print(f"keychain    : password {'present' if has_stored_password(config.login) else 'absent'}")
    if config.missing():
        print(f"missing     : {', '.join(config.missing())}")
        return 1
    try:
        password = get_password(config.login)
    except CredentialsError as exc:
        print(f"IMAP login  : skipped ({exc})")
        return 1
    status = try_imap_login(config, password)
    print(f"IMAP login  : {status}")
    return 0 if status == "OK" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="posta-mcp", description="Local-first email MCP server.")
    parser.add_argument("--version", action="version", version=f"posta-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(
        fn=cmd_serve
    )
    sub.add_parser("setup", help="store credentials and write config").set_defaults(fn=cmd_setup)
    sub.add_parser("doctor", help="check config, keychain and IMAP login").set_defaults(
        fn=cmd_doctor
    )
    args = parser.parse_args(argv)
    return getattr(args, "fn", cmd_serve)(args)


if __name__ == "__main__":
    sys.exit(main())
