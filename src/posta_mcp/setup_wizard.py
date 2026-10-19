"""Pieces of the interactive commands: questions, account detection, Claude Code registration.

The commands themselves (setup, parola, forget, doctor) live in `posta_mcp.cli`. Nothing in this
module ever sees the password; the password prompt and login test stay in the CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from posta_mcp.config import Config, ConfigError, load_config, preview_config

MAX_TRIES = 3
SERVER_NAME = "posta"  # the name the server is registered under in Claude Code
CLAUDE_TIMEOUT = 15  # seconds; `claude mcp get` may start the server to check it

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]{2,}")
_HOST = re.compile(r"[A-Za-z0-9.-]+")
_ACCOUNT_TYPES = {"student": "öğrenci", "staff": "personel"}
_YES = {"e", "evet", "y", "yes"}
_NO = {"h", "hayır", "hayir", "n", "no"}
_UVX = ["uvx", "posta-mcp", "serve"]


# --------------------------------------------------------------------------- questions


def ask_yes(question: str, default: bool) -> bool:
    """A yes/no question; Enter picks the default, shown in capitals."""
    suffix = "[E/h]" if default else "[e/H]"
    for _ in range(MAX_TRIES):
        answer = input(f"{question} {suffix} ").strip().casefold()
        if not answer:
            return default
        if answer in _YES:
            return True
        if answer in _NO:
            return False
        print("Lütfen 'e' (evet) ya da 'h' (hayır) yaz.")
    return default


def looks_like_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL.fullmatch(email))


def account_type(variant: str) -> str:
    return _ACCOUNT_TYPES.get(variant, variant)


def bogazici_account(email: str) -> Config | None:
    """The `bogazici` preset resolved for `email`, or None if no account type has its domain."""
    try:
        preview = preview_config("bogazici", email)
    except ConfigError:
        return None
    return preview if preview.variant else None


def print_hosts(config: Config) -> None:
    imap, smtp = config.imap, config.smtp
    print(f"  Okuma (IMAP)    : {imap.host}:{imap.port} ({imap.security})")
    print(f"  Gönderme (SMTP) : {smtp.host}:{smtp.port} ({smtp.security})")


def ask_account() -> tuple[str, str, Config] | None:
    """Ask for the email address and pick the preset from it: (preset, email, preview)."""
    for _ in range(MAX_TRIES):
        email = input("E-posta adresin: ").strip()
        if not looks_like_email(email):
            print("Bu bir e-posta adresine benzemiyor (örnek: ad.soyad@std.bogazici.edu.tr).")
            continue
        if preview := bogazici_account(email):
            print(f"Boğaziçi {account_type(preview.variant)} hesabı. Sunucular:")
            print_hosts(preview)
            if any(field.startswith(("imap.", "smtp.")) for field in preview.unverified):
                print("  (Bu sunucu ayarları henüz denenmedi; birazdan giriş denenecek.)")
            return "bogazici", email, preview
        question = "Bu adres Boğaziçi adresi değil. Genel IMAP/SMTP ayarlarıyla devam edilsin mi?"
        if ask_yes(question, default=True):
            return "generic", email, preview_config("generic", email)
        print("Tamam, adresi yeniden yaz.")
    return None


def ask_hosts() -> dict[str, str] | None:
    """IMAP and SMTP host names for the generic preset (ports stay at the preset defaults)."""
    hosts: dict[str, str] = {}
    for proto, prompt in (
        ("imap", "IMAP sunucusu (posta okuma, ör. imap.ornek.com): "),
        ("smtp", "SMTP sunucusu (posta gönderme, ör. smtp.ornek.com): "),
    ):
        for _ in range(MAX_TRIES):
            host = input(prompt).strip()
            if _HOST.fullmatch(host) and "." in host:
                hosts[proto] = host
                break
            print("Bu bir sunucu adına benzemiyor (örnek: imap.ornek.com).")
        else:
            return None
    return hosts


def ask_username(default: str) -> str:
    print(f"Giriş için kullanıcı adın büyük ihtimalle: {default}")
    return input("Doğruysa sadece Enter'a bas, değilse doğrusunu yaz: ").strip() or default


def existing_setup_choice(path: Path) -> tuple[str, str]:
    """Menu shown when a config file exists: ("1"|"2"|"3", login of the current account)."""
    login = ""
    try:
        current = load_config()
        login = current.login
        print(f"Bu bilgisayarda kayıtlı bir hesap var: {current.email or '(adres yok)'}")
    except ConfigError as exc:
        print(f"Bu bilgisayarda bir ayar dosyası var ama okunamadı: {exc}")
    print(f"  (ayar dosyası: {path})")
    print("  1) Sadece parolayı güncelle")
    print("  2) Baştan kur")
    print("  3) Çık")
    for _ in range(MAX_TRIES):
        answer = input("Seçimin [1]: ").strip() or "1"
        if answer in {"1", "2", "3"}:
            return answer, login
        print("Lütfen 1, 2 ya da 3 yaz.")
    return "3", login


def build_config(preset: str, email: str, username: str, hosts: dict[str, str]) -> Config:
    """What config.toml written with these values would load to (ignoring file and env)."""
    data = preview_config(preset, email).model_dump()
    data["username"] = username
    for proto, host in hosts.items():
        data[proto]["host"] = host
    return Config.model_validate(data)


# --------------------------------------------------------------------------- serve command


def serve_command_for(direct_url: str | None, executable: str | None = None) -> list[str]:
    """How an MCP client should start this installation, from its PEP 610 direct_url.json.

    None (no direct_url) means a registry install (PyPI): `uvx posta-mcp serve`. A VCS URL means
    `uvx --from git+...`; a local directory means a source checkout run with `uv run`. Anything
    else falls back to the running executable's absolute path, else to the PyPI form.
    """
    if direct_url is None:
        return list(_UVX)
    try:
        info = json.loads(direct_url)
    except ValueError:
        info = {}
    info = info if isinstance(info, dict) else {}
    url = info.get("url")
    url = url if isinstance(url, str) else ""
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict) and url:
        spec = f"{vcs.get('vcs', 'git')}+{url}"
        if revision := vcs.get("requested_revision"):
            spec += f"@{revision}"
        if subdirectory := info.get("subdirectory"):
            spec += f"#subdirectory={subdirectory}"
        return ["uvx", "--from", spec, "posta-mcp", "serve"]
    if isinstance(info.get("dir_info"), dict) and url.startswith("file:"):
        project = _file_url_path(url)
        if (project / "pyproject.toml").is_file():
            return ["uv", "run", "--directory", str(project), "posta-mcp", "serve"]
    if executable:
        return [os.path.abspath(executable), "serve"]
    return list(_UVX)


def _file_url_path(url: str) -> Path:
    parsed = urlparse(url)
    unc = f"//{parsed.netloc}" if parsed.netloc not in ("", "localhost") else ""
    return Path(url2pathname(unc + parsed.path))  # url2pathname also percent-decodes


def _direct_url_text() -> str | None:
    """direct_url.json of the installed distribution; raises PackageNotFoundError if none."""
    return metadata.distribution("posta-mcp").read_text("direct_url.json")


def serve_command() -> list[str]:
    try:
        text = _direct_url_text()
    except metadata.PackageNotFoundError:
        text = "{}"  # not installed as a distribution: use the executable fallback
    return serve_command_for(text, shutil.which("posta-mcp"))


def own_command(subcommand: str) -> str:
    """This installation's command line for a posta-mcp subcommand, e.g. `uvx posta-mcp parola`."""
    return format_command([*serve_command()[:-1], subcommand])


def format_command(args: list[str]) -> str:
    """A copy-pasteable command line (quotes only arguments with spaces)."""
    return " ".join(f'"{a}"' if not a or any(c.isspace() for c in a) else a for a in args)


# --------------------------------------------------------------------------- Claude Code


def find_claude() -> str | None:
    return shutil.which("claude")


def run_claude(
    claude: str, args: list[str], timeout: float = CLAUDE_TIMEOUT
) -> subprocess.CompletedProcess[str] | None:
    """Run the claude CLI (list args, no shell). None if it could not be run in time."""
    try:
        return subprocess.run(
            [claude, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def claude_has_server(claude: str) -> bool | None:
    """Whether Claude Code knows a server named `posta`; None if that could not be checked."""
    result = run_claude(claude, ["mcp", "get", SERVER_NAME])
    return None if result is None else result.returncode == 0


def add_args(command: list[str]) -> list[str]:
    return ["mcp", "add", "--scope", "user", SERVER_NAME, "--", *command]


def remove_args() -> list[str]:
    return ["mcp", "remove", SERVER_NAME, "--scope", "user"]


def print_manual_registration(command: list[str]) -> None:
    print("Claude Code'a eklemek için şu komutu çalıştır:")
    print(f"  {format_command(['claude', *add_args(command)])}")
    print("Claude Desktop kullanıyorsan bunun yerine .mcpb dosyasını kur (README'de anlatılıyor).")


def _why(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "claude komutu çalıştırılamadı"
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return lines[-1][:200] if lines else f"çıkış kodu {result.returncode}"


def offer_claude_code(command: list[str]) -> bool:
    """Offer to register the server with Claude Code (user scope). True if it is registered now.

    Only `command` (no email, no password) goes on the claude command line; the account settings
    are read from config.toml and the keychain when the server starts.
    """
    claude = find_claude()
    if claude is None:
        print_manual_registration(command)
        return False
    if claude_has_server(claude):
        print(f"'{SERVER_NAME}' zaten Claude Code'a ekli.")
        if not ask_yes("Bu kurulumla değiştireyim mi?", default=True):
            print("Claude Code ayarına dokunmadım.")
            return True
        removed = run_claude(claude, remove_args())
        if removed is None or removed.returncode != 0:
            print(f"Eski kayıt kaldırılamadı ({_why(removed)}).")
            print(f"Elle kaldırıp yeniden ekle: {format_command(['claude', *remove_args()])}")
            print_manual_registration(command)
            return True
    elif not ask_yes("Claude Code'a şimdi ekleyeyim mi?", default=True):
        print_manual_registration(command)
        return False
    result = run_claude(claude, add_args(command), timeout=60)
    if result is None or result.returncode != 0:
        print(f"Claude Code'a eklenemedi ({_why(result)}).")
        print_manual_registration(command)
        return False
    print("Claude Code'a eklendi. Claude Code'u yeniden başlat ve /mcp ile bağlantıya bak.")
    return True
