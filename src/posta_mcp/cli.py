"""Command line: `posta-mcp [serve] | setup | parola | doctor | forget | --version`.

User-facing text is Turkish. The password is only ever read with getpass and goes to the OS
keychain; it is never echoed, written to a file or put on a command line.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from posta_mcp import __version__, setup_wizard
from posta_mcp.config import Config, ConfigError, config_path, load_config
from posta_mcp.credentials import (
    CredentialsError,
    delete_password,
    get_password,
    has_stored_password,
    set_password,
)
from posta_mcp.logs import configure_logging, redact, register_secret
from posta_mcp.profile import profile_path
from posta_mcp.setup_wizard import MAX_TRIES, ask_yes, own_command
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
    """Log in once over verified TLS. Returns a short status line; never includes the password."""
    from imapclient import IMAPClient

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
        return redact(f"FAILED ({type(exc).__name__}: {exc})", password)


def cmd_serve(_: argparse.Namespace) -> int:
    configure_logging()
    from posta_mcp.server import run

    try:
        config = load_config()
    except ConfigError as exc:
        return _fail(str(exc))
    if config.missing():
        return _fail(f"missing settings: {', '.join(config.missing())}. Run `posta-mcp setup`.")
    run(config)
    return 0


def _note_env_password() -> None:
    if os.environ.get("POSTA_PASSWORD"):
        print(
            "Not: POSTA_PASSWORD ortam değişkeni tanımlı. O tanımlıyken anahtar zincirine "
            "kaydedilen parola kullanılmaz."
        )


def _password_with_login_test(config: Config) -> tuple[str | None, bool]:
    """Ask for the password (up to MAX_TRIES times) and test it with an IMAP login.

    Returns (password, True) after a working login, (password, False) when the user chose to keep
    a password that could not be confirmed, and (None, False) when nothing should be saved.
    """
    password = ""
    for attempt in range(1, MAX_TRIES + 1):
        password = getpass.getpass("Parolan (yazarken görünmez): ")
        register_secret(password)
        if not password:
            print("Parola boş olamaz.")
        else:
            print("Giriş deneniyor...")
            status = try_imap_login(config, password)
            if status == "OK":
                print("Giriş başarılı.")
                return password, True
            print(f"Giriş olmadı: {status}")
        if attempt == MAX_TRIES or not ask_yes("Parolayı yeniden girmek ister misin?", True):
            break
    question = "Yine de kaydedilsin mi? (örneğin şu an VPN'e bağlı değilsen)"
    if password and ask_yes(question, default=False):
        return password, False
    return None, False


def _forget_old_password(login: str) -> None:
    """Drop a keychain entry the new setup no longer uses (best effort)."""
    try:
        delete_password(login)
    except CredentialsError:
        pass


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive setup. Nothing is saved before the login was tested, unless the user explicitly
    keeps an untested password. Exit code: 0 after a working login (or when the user picks "çık"),
    1 when the saved password could not be confirmed or the user saved nothing after a failed
    login, 2 on unusable answers or a keychain error.
    """
    path = config_path()
    old_login = ""
    if path.exists():
        choice, old_login = setup_wizard.existing_setup_choice(path)
        if choice == "1":
            return cmd_parola(args)
        if choice == "3":
            print("Tamam, hiçbir şey değiştirilmedi.")
            return 0
    print("posta-mcp kurulumu. Parolan sadece bilgisayarının anahtar zincirine kaydedilir.")
    account = setup_wizard.ask_account()
    if account is None:
        return _fail("geçerli bir e-posta adresi alınamadı; hiçbir şey kaydedilmedi.")
    preset, email, preview = account
    hosts: dict[str, str] = {}
    if preset == "generic":
        if (asked := setup_wizard.ask_hosts()) is None:
            return _fail("sunucu adı alınamadı; hiçbir şey kaydedilmedi.")
        hosts = asked
    username = setup_wizard.ask_username(preview.login)
    try:
        config = setup_wizard.build_config(preset, email, username, hosts)
    except ConfigError as exc:
        return _fail(f"{exc} Hiçbir şey kaydedilmedi.")
    if preset == "generic":
        print("Sunucular:")
        setup_wizard.print_hosts(config)
    _note_env_password()
    password, ok = _password_with_login_test(config)
    if password is None:
        print("Hiçbir şey kaydedilmedi.")
        return 1
    try:
        set_password(config.login, password)
    except CredentialsError as exc:
        return _fail(f"parola kaydedilemedi ({exc}); ayar dosyası da yazılmadı.")
    # only record a username that differs from what the preset derives from the email
    _write_config(path, preset, email, "" if username == preview.login else username, hosts)
    if old_login and old_login != config.login:
        _forget_old_password(old_login)
    print(f"Ayarlar kaydedildi: {path} (içinde parola yok). Parola anahtar zincirinde.")
    if not ok:
        print(f"Giriş doğrulanmadı. Bağlantın düzelince dene: {own_command('doctor')}")
    setup_wizard.offer_claude_code(setup_wizard.serve_command())
    if ok:
        print(f"Bir sorun olursa kontrol için: {own_command('doctor')}")
    return 0 if ok else 1


def cmd_parola(_: argparse.Namespace) -> int:
    """Re-ask the password of the configured account, test it, store it. Exit codes as setup."""
    try:
        config = load_config()
    except ConfigError as exc:
        return _fail(f"{exc} Baştan kurmak için: {own_command('setup')}")
    if config.missing():
        return _fail(f"henüz kurulmuş bir hesap yok. Önce: {own_command('setup')}")
    print(f"Hesap: {config.email} (kullanıcı adı: {config.login})")
    _note_env_password()
    password, ok = _password_with_login_test(config)
    if password is None:
        print("Parola değiştirilmedi.")
        return 1
    try:
        set_password(config.login, password)
    except CredentialsError as exc:
        return _fail(f"parola kaydedilemedi ({exc}).")
    print("Yeni parola anahtar zincirine kaydedildi.")
    return 0 if ok else 1


def cmd_forget(_: argparse.Namespace) -> int:
    """Remove this computer's account data: keychain entry, config file, optionally the profile."""
    path = config_path()
    login = email = ""
    try:
        config = load_config()
        login, email = config.login, config.email
    except ConfigError as exc:
        print(f"Ayar dosyası okunamadı ({exc}); parolanın hangi hesaba ait olduğu bilinmiyor.")
    has_file = path.is_file()
    has_password = bool(login) and has_stored_password(login)
    status = 0
    if has_file or has_password:
        print(f"Hesap: {email or '(bilinmiyor)'}. Şunlar silinecek:")
        if has_password:
            print(f"  - anahtar zincirindeki parola ({login})")
        if has_file:
            print(f"  - ayar dosyası: {path}")
        if not ask_yes("Silinsin mi?", default=False):
            print("Hiçbir şey silinmedi.")
            return 0
        if has_password:
            try:
                delete_password(login)
                print("Parola anahtar zincirinden silindi.")
            except CredentialsError as exc:
                print(f"posta-mcp: {exc}", file=sys.stderr)
                status = 1
        if has_file:
            path.unlink()
            print("Ayar dosyası silindi.")
    else:
        print("Bu bilgisayarda kayıtlı bir hesap bulunamadı.")
    profile = profile_path()
    if profile.is_file() and ask_yes(f"İlgi profilin de silinsin mi? ({profile})", False):
        profile.unlink()
        print("İlgi profili silindi.")
    claude = setup_wizard.find_claude()
    if claude and setup_wizard.claude_has_server(claude):
        if ask_yes("posta, Claude Code'dan da çıkarılsın mı?", default=True):
            result = setup_wizard.run_claude(claude, setup_wizard.remove_args())
            if result is not None and result.returncode == 0:
                print("Claude Code'dan çıkarıldı.")
            else:
                command = setup_wizard.format_command(["claude", *setup_wizard.remove_args()])
                print(f"Claude Code'dan çıkarılamadı. Elle dene: {command}")
                status = 1
    return status


_LABEL = 18


def _line(label: str, value: str) -> None:
    print(f"{label:<{_LABEL}}: {value}")


def _hint(text: str) -> None:
    print(f"{'':<{_LABEL}}  → Ne yapmalı: {text}")


def _doctor_claude_code() -> None:
    """Report whether Claude Code has the server; never fails the doctor run."""
    claude = setup_wizard.find_claude()
    if claude is None:
        return
    registered = setup_wizard.claude_has_server(claude)
    if registered is None:
        _line("Claude Code", "kontrol edilemedi (claude komutu yanıt vermedi)")
        _hint("Claude Code içinde /mcp yazıp 'posta' sunucusuna bak")
    elif registered:
        _line("Claude Code", f"'{setup_wizard.SERVER_NAME}' ekli")
    else:
        _line("Claude Code", f"'{setup_wizard.SERVER_NAME}' ekli değil")
        command = ["claude", *setup_wizard.add_args(setup_wizard.serve_command())]
        _hint(f"{setup_wizard.format_command(command)} (ya da kurulumu yeniden çalıştır)")


def cmd_doctor(_: argparse.Namespace) -> int:
    print(f"posta-mcp {__version__}")
    path = config_path()
    try:
        config = load_config()
    except ConfigError as exc:
        _line("Ayar dosyası", f"{path} (okunamadı)")
        print(f"{'':<{_LABEL}}  {exc}")
        _hint(f"{own_command('setup')} (baştan kurar, dosyanın üstüne yazar)")
        _doctor_claude_code()
        return 2
    missing = config.missing()
    found = path.is_file()
    _line("Ayar dosyası", f"{path} ({'var' if found else 'yok'})")
    if missing and not found:  # nothing set up yet: the details below would only be noise
        _hint(f"{own_command('setup')} (hesabını kurar)")
        _doctor_claude_code()
        return 1
    account = setup_wizard.account_type(config.variant) or "genel IMAP/SMTP"
    _line("Kurum ayarı", config.preset)
    _line("Hesap", f"{config.email or '(yok)'} ({account})")
    _line("Kullanıcı adı", config.login or "(yok)")
    _line("Okuma (IMAP)", f"{config.imap.host}:{config.imap.port} ({config.imap.security})")
    _line("Gönderme (SMTP)", f"{config.smtp.host}:{config.smtp.port} ({config.smtp.security})")
    _line("Posta gönderme", "AÇIK (her posta onay ister)" if config.send_enabled else "kapalı")
    _line("Denenmemiş ayar", ", ".join(config.unverified) or "yok")
    if missing:
        _line("Eksik ayar", ", ".join(missing))
        _hint(f"{own_command('setup')} (eksikleri sorar)")
        _doctor_claude_code()
        return 1
    if os.environ.get("POSTA_PASSWORD"):
        _line("Parola", "POSTA_PASSWORD ortam değişkeninden")
    elif has_stored_password(config.login):
        _line("Parola", "anahtar zincirinde kayıtlı")
    else:
        _line("Parola", "anahtar zincirinde yok")
        _hint(f"{own_command('parola')} (parolanı sorar ve kaydeder)")
        _doctor_claude_code()
        return 1
    ok = False
    try:
        password = get_password(config.login)
    except CredentialsError as exc:
        _line("IMAP girişi", f"denenmedi ({exc})")
        _hint(f"{own_command('parola')} (parolanı sorar ve kaydeder)")
    else:
        status = try_imap_login(config, password)
        _line("IMAP girişi", status)
        ok = status == "OK"
        if not ok:
            _hint(
                f"parolan değiştiyse ya da süresi dolduysa {own_command('parola')}; "
                "bağlantı kurulamıyorsa internetini ya da VPN'i kontrol et"
            )
    _doctor_claude_code()
    return 0 if ok else 1


class _HelpFormatter(argparse.HelpFormatter):
    """argparse help with a Turkish usage line."""

    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[no-untyped-def]
        super().add_usage(usage, actions, groups, "kullanım: " if prefix is None else prefix)


def _turkish_help(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser._optionals.title = "seçenekler"  # argparse has no public setter for this title
    parser.add_argument("-h", "--help", action="help", help="bu yardımı göster")
    return parser


_COMMANDS = (
    ("serve", (), "MCP sunucusunu başlat (Claude bunu kendisi çalıştırır; varsayılan)"),
    ("setup", (), "hesabını kur: adres, parola, giriş denemesi, Claude Code'a ekleme"),
    ("parola", ("password",), "sadece parolanı güncelle (örneğin süresi dolduysa)"),
    ("doctor", (), "ayarları, parolayı ve girişi kontrol et"),
    ("forget", ("kaldir",), "bu bilgisayardaki hesap bilgilerini sil"),
)


def _ensure_printable_output() -> None:
    """Turkish output must not crash on a pipe with a legacy code page (e.g. cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            "ğüşıöçİ→".encode(stream.encoding or "ascii")
        except (LookupError, UnicodeEncodeError):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] != "serve":  # serve's stdout belongs to the MCP protocol
        _ensure_printable_output()
    fns = {
        "serve": cmd_serve,
        "setup": cmd_setup,
        "parola": cmd_parola,
        "doctor": cmd_doctor,
        "forget": cmd_forget,
    }
    parser = _turkish_help(
        argparse.ArgumentParser(
            prog="posta-mcp",
            description="Claude'un e-postanı okumasını sağlayan yerel MCP sunucusu.",
            epilog="Komut yazmazsan sunucu başlar (serve). İlk kurulum için: posta-mcp setup",
            formatter_class=_HelpFormatter,
            add_help=False,
        )
    )
    parser.add_argument(
        "--version", action="version", version=f"posta-mcp {__version__}", help="sürümü göster"
    )
    sub = parser.add_subparsers(dest="command", title="komutlar", metavar="KOMUT")
    for name, aliases, help_text in _COMMANDS:
        command = sub.add_parser(
            name,
            aliases=list(aliases),
            help=help_text,
            description=help_text,
            formatter_class=_HelpFormatter,
            add_help=False,
        )
        _turkish_help(command).set_defaults(fn=fns[name])
    args = parser.parse_args(argv)
    fn = getattr(args, "fn", cmd_serve)
    if fn is cmd_serve:
        return fn(args)
    try:
        return fn(args)
    except (KeyboardInterrupt, EOFError):
        print("\nİptal edildi.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
