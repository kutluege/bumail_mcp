import os
import subprocess
from importlib import metadata

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from posta_mcp import __version__, logs, setup_wizard
from posta_mcp.cli import _write_config, main
from posta_mcp.config import load_config
from posta_mcp.credentials import SERVICE

STUDENT = "ada.lovelace@std.bogazici.edu.tr"
UVX_ADD = ["mcp", "add", "--scope", "user", "posta", "--", "uvx", "posta-mcp", "serve"]


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("not found")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """A clean environment: config in tmp_path, memory keychain, no claude CLI, PyPI install."""
    for name in list(os.environ):
        if name.startswith("POSTA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("POSTA_CONFIG", str(tmp_path / "config.toml"))
    backend = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_wizard, "_direct_url_text", lambda: None)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("unexpected subprocess call")

    monkeypatch.setattr(setup_wizard.subprocess, "run", no_subprocess)
    # scripted passwords get registered for redaction; keep them out of other tests
    monkeypatch.setattr(logs, "_secrets", set())
    yield backend
    keyring.set_keyring(old)


@pytest.fixture
def config_file(tmp_path):
    return tmp_path / "config.toml"


class Script:
    """Scripted answers for input() and getpass(), plus recorded login attempts."""

    def __init__(self, monkeypatch, answers=(), passwords=(), logins=()):
        self.answers, self.passwords, self.logins = list(answers), list(passwords), list(logins)
        self.prompts: list[str] = []
        self.tried: list[tuple[object, str]] = []
        monkeypatch.setattr("builtins.input", self._input)
        monkeypatch.setattr("getpass.getpass", self._getpass)
        monkeypatch.setattr("posta_mcp.cli.try_imap_login", self._login)

    def _input(self, prompt=""):
        self.prompts.append(prompt.strip())
        return self.answers.pop(0)

    def _getpass(self, prompt=""):
        self.prompts.append(prompt.strip())
        return self.passwords.pop(0)

    def _login(self, config, password):
        self.tried.append((config, password))
        return self.logins.pop(0)


class FakeClaude:
    """A `claude` CLI on PATH; records calls and answers `mcp get` with `registered`."""

    def __init__(self, monkeypatch, registered=False, fail=()):
        self.registered, self.fail = registered, set(fail)
        self.calls: list[list[str]] = []
        monkeypatch.setattr(
            setup_wizard.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None
        )
        monkeypatch.setattr(setup_wizard.subprocess, "run", self._run)

    def _run(self, args, **kwargs):
        assert kwargs.get("timeout") and "shell" not in kwargs
        self.calls.append(args[1:])
        verb = args[2]
        code = 1 if verb in self.fail else 0
        if verb == "get":
            code = 0 if self.registered else 1
        return subprocess.CompletedProcess(args, code, stdout="", stderr="hata satırı\n")


def stored(backend) -> dict[str, str]:
    return {user: pw for (service, user), pw in backend.store.items() if service == SERVICE}


# --------------------------------------------------------------------------- basics


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_is_turkish(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "kullanım:" in out and "komutlar" in out
    assert "parola (password)" in out and "forget (kaldir)" in out


def test_written_config_has_no_secret_and_round_trips(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    _write_config(path, "generic", "ü@example.org", "login", {"imap": "i.example.org"})
    text = path.read_text(encoding="utf-8")
    assert "password" not in text.lower()
    cfg = load_config(env={}, path=path)
    assert (cfg.email, cfg.username, cfg.imap.host) == ("ü@example.org", "login", "i.example.org")


# --------------------------------------------------------------------------- setup


def test_setup_asks_email_first_and_detects_student(monkeypatch, capsys, env, config_file):
    seen_before_password: list[str] = []
    script = Script(monkeypatch, answers=[STUDENT, ""], passwords=["pw-setup-test"], logins=["OK"])
    original = script._getpass

    def getpass_after_hosts(prompt=""):
        seen_before_password.append(capsys.readouterr().out)
        return original(prompt)

    monkeypatch.setattr("getpass.getpass", getpass_after_hosts)
    assert main(["setup"]) == 0
    assert script.prompts[0] == "E-posta adresin:"
    assert "Boğaziçi öğrenci hesabı" in seen_before_password[0]
    assert "imap.std.bogazici.edu.tr:993" in seen_before_password[0]
    assert "smtp.std.bogazici.edu.tr:587 (starttls)" in seen_before_password[0]
    assert "Enter" in script.prompts[1]
    assert stored(env) == {"ada.lovelace": "pw-setup-test"}  # localpart login
    config, password = script.tried[0]
    assert (config.login, config.imap.host, password) == (
        "ada.lovelace",
        "imap.std.bogazici.edu.tr",
        "pw-setup-test",
    )
    text = config_file.read_text(encoding="utf-8")
    assert 'preset = "bogazici"' in text
    assert "username" not in text and "pw-setup-test" not in text
    out = capsys.readouterr().out
    assert "claude mcp add --scope user posta -- uvx posta-mcp serve" in out  # no claude CLI
    assert ".mcpb" in out
    assert "pw-setup-test" not in out


def test_setup_reasks_bad_or_foreign_email(monkeypatch, capsys, env):
    answers = ["not-an-email", "me@gmail.com", "h", STUDENT, ""]
    script = Script(monkeypatch, answers=answers, passwords=["pw"], logins=["OK"])
    assert main(["setup"]) == 0
    assert "e-posta adresine benzemiyor" in capsys.readouterr().out
    assert script.prompts[2].startswith("Bu adres Boğaziçi adresi değil.")
    assert script.prompts[3] == "E-posta adresin:"  # declined generic: asked again
    assert stored(env) == {"ada.lovelace": "pw"}


def test_setup_gives_up_after_three_bad_emails(monkeypatch, capsys, env, config_file):
    Script(monkeypatch, answers=["a", "b@", "c@d"])
    assert main(["setup"]) == 2
    assert "hiçbir şey kaydedilmedi" in capsys.readouterr().err
    assert not config_file.exists() and stored(env) == {}


def test_setup_generic_asks_hosts(monkeypatch, capsys, env, config_file):
    answers = ["me@example.org", "", "imap.example.org", "smtp.example.org", ""]
    script = Script(monkeypatch, answers=answers, passwords=["pw"], logins=["OK"])
    assert main(["setup"]) == 0
    assert "Boğaziçi adresi değil" in script.prompts[1]
    config, _ = script.tried[0]
    assert (config.preset, config.login, config.imap.host) == (
        "generic",
        "me@example.org",
        "imap.example.org",
    )
    cfg = load_config(env={}, path=config_file)
    assert (cfg.preset, cfg.imap.host, cfg.smtp.host) == (
        "generic",
        "imap.example.org",
        "smtp.example.org",
    )
    assert stored(env) == {"me@example.org": "pw"}


def test_setup_custom_username_is_used_and_saved(monkeypatch, env, config_file):
    script = Script(monkeypatch, answers=[STUDENT, STUDENT], passwords=["pw"], logins=["OK"])
    assert main(["setup"]) == 0
    assert script.tried[0][0].login == STUDENT
    assert load_config(env={}, path=config_file).login == STUDENT
    assert stored(env) == {STUDENT: "pw"}


def test_setup_login_failure_then_retry_succeeds(monkeypatch, capsys, env, config_file):
    script = Script(
        monkeypatch,
        answers=[STUDENT, "", "e"],
        passwords=["wrong-pw", "right-pw"],
        logins=["Kullanıcı adı ya da parola yanlış.", "OK"],
    )
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Giriş olmadı: Kullanıcı adı ya da parola yanlış." in out
    assert "Parolayı yeniden girmek ister misin? [E/h]" in script.prompts
    assert [pw for _, pw in script.tried] == ["wrong-pw", "right-pw"]
    assert stored(env) == {"ada.lovelace": "right-pw"}
    assert config_file.exists()


def test_setup_save_anyway_after_failed_login(monkeypatch, capsys, env, config_file):
    script = Script(
        monkeypatch, answers=[STUDENT, "", "h", "e"], passwords=["pw"], logins=["zaman aşımı"]
    )
    assert main(["setup"]) == 1  # saved, but the login is not confirmed
    assert "Yine de kaydedilsin mi? (örneğin şu an VPN'e bağlı değilsen) [e/H]" in script.prompts
    assert stored(env) == {"ada.lovelace": "pw"}
    assert config_file.exists()
    assert "Giriş doğrulanmadı" in capsys.readouterr().out


def test_setup_writes_nothing_when_user_declines(monkeypatch, capsys, env, config_file):
    Script(monkeypatch, answers=[STUDENT, "", "h", ""], passwords=["pw"], logins=["hata"])
    assert main(["setup"]) == 1
    assert "Hiçbir şey kaydedilmedi" in capsys.readouterr().out
    assert not config_file.exists() and stored(env) == {}


def test_setup_stops_retrying_after_three_passwords(monkeypatch, env, config_file):
    script = Script(
        monkeypatch,
        answers=[STUDENT, "", "e", "e", "h"],
        passwords=["a", "b", "c"],
        logins=["hata", "hata", "hata"],
    )
    assert main(["setup"]) == 1
    assert len(script.tried) == 3
    assert script.prompts.count("Parolayı yeniden girmek ister misin? [E/h]") == 2
    assert not config_file.exists() and stored(env) == {}


def test_setup_ctrl_c_saves_nothing(monkeypatch, capsys, env, config_file):
    Script(monkeypatch, answers=[STUDENT, ""], logins=["OK"])

    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", interrupt)
    assert main(["setup"]) == 130
    assert "İptal edildi" in capsys.readouterr().err
    assert not config_file.exists() and stored(env) == {}


# --------------------------------------------------------------------------- existing config


@pytest.fixture
def student_config(config_file, env):
    _write_config(config_file, "bogazici", STUDENT, "", {})
    env.set_password(SERVICE, "ada.lovelace", "old-pw")
    return config_file


def test_existing_config_menu_updates_password_only(monkeypatch, capsys, env, student_config):
    before = student_config.read_text(encoding="utf-8")
    script = Script(monkeypatch, answers=["1"], passwords=["new-pw"], logins=["OK"])
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert STUDENT in out and "1) Sadece parolayı güncelle" in out and "3) Çık" in out
    assert script.prompts[0] == "Seçimin [1]:"
    assert stored(env) == {"ada.lovelace": "new-pw"}
    assert student_config.read_text(encoding="utf-8") == before


def test_existing_config_menu_quit_changes_nothing(monkeypatch, env, student_config):
    before = student_config.read_text(encoding="utf-8")
    script = Script(monkeypatch, answers=["3"])
    assert main(["setup"]) == 0
    assert script.tried == []
    assert student_config.read_text(encoding="utf-8") == before
    assert stored(env) == {"ada.lovelace": "old-pw"}


def test_existing_config_menu_fresh_setup_replaces_account(monkeypatch, env, student_config):
    other = "bob.smith@std.bogazici.edu.tr"
    Script(monkeypatch, answers=["2", other, ""], passwords=["bob-pw"], logins=["OK"])
    assert main(["setup"]) == 0
    assert load_config(env={}, path=student_config).email == other
    assert stored(env) == {"bob.smith": "bob-pw"}  # the old entry is gone


def test_parola_updates_password(monkeypatch, capsys, env, student_config):
    script = Script(monkeypatch, passwords=["new-pw"], logins=["OK"])
    assert main(["parola"]) == 0
    assert script.tried[0][0].login == "ada.lovelace"
    assert stored(env) == {"ada.lovelace": "new-pw"}
    assert "new-pw" not in capsys.readouterr().out


def test_password_alias_keeps_old_password_on_decline(monkeypatch, env, student_config):
    Script(monkeypatch, answers=["h", "h"], passwords=["bad"], logins=["hata"])
    assert main(["password"]) == 1
    assert stored(env) == {"ada.lovelace": "old-pw"}


def test_parola_without_setup_points_to_setup(monkeypatch, capsys):
    Script(monkeypatch)
    assert main(["parola"]) == 2
    assert "posta-mcp setup" in capsys.readouterr().err


# --------------------------------------------------------------------------- forget


def test_forget_removes_everything_confirmed(monkeypatch, tmp_path, env, student_config):
    profile = tmp_path / "profile.toml"
    profile.write_text('notes = "x"\n', encoding="utf-8")
    claude = FakeClaude(monkeypatch, registered=True)
    Script(monkeypatch, answers=["e", "e", ""])
    assert main(["forget"]) == 0
    assert not student_config.exists() and not profile.exists()
    assert stored(env) == {}
    assert claude.calls == [["mcp", "get", "posta"], ["mcp", "remove", "posta", "--scope", "user"]]


def test_forget_keeps_profile_unless_asked(monkeypatch, tmp_path, env, student_config):
    profile = tmp_path / "profile.toml"
    profile.write_text('notes = "x"\n', encoding="utf-8")
    Script(monkeypatch, answers=["e", ""])
    assert main(["kaldir"]) == 0
    assert not student_config.exists() and profile.exists()


def test_forget_declined_deletes_nothing(monkeypatch, env, student_config):
    Script(monkeypatch, answers=[""])
    assert main(["forget"]) == 0
    assert student_config.exists() and stored(env) == {"ada.lovelace": "old-pw"}


def test_forget_without_keychain_entry(monkeypatch, env, student_config):
    env.store.clear()
    Script(monkeypatch, answers=["e"])
    assert main(["forget"]) == 0
    assert not student_config.exists()


# --------------------------------------------------------------------------- Claude Code


def test_setup_registers_with_claude_code(monkeypatch, capsys, env):
    claude = FakeClaude(monkeypatch, registered=False)
    script = Script(monkeypatch, answers=[STUDENT, "", ""], passwords=["pw-x"], logins=["OK"])
    assert main(["setup"]) == 0
    assert "Claude Code'a şimdi ekleyeyim mi? [E/h]" in script.prompts
    assert claude.calls == [["mcp", "get", "posta"], UVX_ADD]
    assert all("pw-x" not in a and STUDENT not in a for call in claude.calls for a in call)
    assert "Claude Code'a eklendi" in capsys.readouterr().out


def test_claude_code_already_registered_is_replaced(monkeypatch, capsys):
    claude = FakeClaude(monkeypatch, registered=True)
    Script(monkeypatch, answers=[""])
    assert setup_wizard.offer_claude_code(["uvx", "posta-mcp", "serve"])
    assert "zaten Claude Code'a ekli" in capsys.readouterr().out
    assert claude.calls == [
        ["mcp", "get", "posta"],
        ["mcp", "remove", "posta", "--scope", "user"],
        UVX_ADD,
    ]


def test_claude_code_already_registered_kept_on_no(monkeypatch):
    claude = FakeClaude(monkeypatch, registered=True)
    Script(monkeypatch, answers=["h"])
    assert setup_wizard.offer_claude_code(["uvx", "posta-mcp", "serve"])
    assert claude.calls == [["mcp", "get", "posta"]]


def test_claude_code_declined_prints_command(monkeypatch, capsys):
    claude = FakeClaude(monkeypatch, registered=False)
    Script(monkeypatch, answers=["h"])
    assert not setup_wizard.offer_claude_code(["uvx", "posta-mcp", "serve"])
    assert claude.calls == [["mcp", "get", "posta"]]
    out = capsys.readouterr().out
    assert "claude mcp add --scope user posta -- uvx posta-mcp serve" in out and ".mcpb" in out


def test_claude_code_add_failure_prints_command(monkeypatch, capsys):
    FakeClaude(monkeypatch, registered=False, fail={"add"})
    Script(monkeypatch, answers=[""])
    assert not setup_wizard.offer_claude_code(["uvx", "posta-mcp", "serve"])
    out = capsys.readouterr().out
    assert "eklenemedi (hata satırı)" in out
    assert "claude mcp add --scope user posta -- uvx posta-mcp serve" in out


def test_missing_claude_cli_prints_command(capsys):
    assert not setup_wizard.offer_claude_code(["uv", "run", "--directory", "C:\\a b", "x"])
    out = capsys.readouterr().out
    assert 'claude mcp add --scope user posta -- uv run --directory "C:\\a b" x' in out


# --------------------------------------------------------------------------- serve command


def test_serve_command_from_pypi():
    assert setup_wizard.serve_command_for(None) == ["uvx", "posta-mcp", "serve"]


def test_serve_command_from_git():
    url = "https://github.com/kutluege/bogazici_postanesi"
    info = f'{{"url": "{url}", "vcs_info": {{"vcs": "git", "commit_id": "abc123"}}}}'
    assert setup_wizard.serve_command_for(info) == [
        "uvx",
        "--from",
        f"git+{url}",
        "posta-mcp",
        "serve",
    ]
    pinned = info.replace('"commit_id"', '"requested_revision": "v0.1.0", "commit_id"')
    assert setup_wizard.serve_command_for(pinned)[2] == f"git+{url}@v0.1.0"


def test_serve_command_from_source_checkout(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    info = f'{{"url": "{tmp_path.as_uri()}", "dir_info": {{"editable": true}}}}'
    command = setup_wizard.serve_command_for(info)
    assert command[:3] == ["uv", "run", "--directory"] and command[4:] == ["posta-mcp", "serve"]
    assert os.path.samefile(command[3], tmp_path)


def test_serve_command_unexpected_falls_back_to_executable(tmp_path):
    archive = '{"url": "https://example.org/posta_mcp.whl", "archive_info": {}}'
    exe = str(tmp_path / "bin" / "posta-mcp")
    assert setup_wizard.serve_command_for(archive, exe) == [exe, "serve"]
    assert setup_wizard.serve_command_for(archive) == ["uvx", "posta-mcp", "serve"]
    gone = f'{{"url": "{(tmp_path / "gone").as_uri()}", "dir_info": {{}}}}'
    assert setup_wizard.serve_command_for(gone, exe) == [exe, "serve"]
    assert setup_wizard.serve_command_for("not json", exe) == [exe, "serve"]


def test_serve_command_when_not_installed(monkeypatch, tmp_path):
    def missing():
        raise metadata.PackageNotFoundError("posta-mcp")

    exe = str(tmp_path / "posta-mcp")
    monkeypatch.setattr(setup_wizard, "_direct_url_text", missing)
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: exe)
    assert setup_wizard.serve_command() == [exe, "serve"]


def test_own_command_uses_install_form():
    assert setup_wizard.own_command("parola") == "uvx posta-mcp parola"


# --------------------------------------------------------------------------- doctor


def test_doctor_without_setup_points_to_setup(capsys):
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "Ayar dosyası" in out and "(yok)" in out
    assert "→ Ne yapmalı: uvx posta-mcp setup" in out


def test_doctor_missing_password_points_to_parola(env, student_config, capsys):
    env.store.clear()
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "anahtar zincirinde yok" in out
    assert "→ Ne yapmalı: uvx posta-mcp parola" in out


def test_doctor_reports_login_failure_without_password(monkeypatch, capsys):
    monkeypatch.setenv("POSTA_PRESET", "bogazici")
    monkeypatch.setenv("POSTA_EMAIL", "x.y@std.bogazici.edu.tr")
    monkeypatch.setenv("POSTA_PASSWORD", "pw-doctor-test")
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "Giriş reddedildi (stub)")
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "Denenmemiş ayar" in out and "domains.student" in out
    assert "(öğrenci)" in out and "Kullanıcı adı     : x.y" in out
    assert "IMAP girişi       : Giriş reddedildi (stub)" in out
    assert "uvx posta-mcp parola" in out
    assert "pw-doctor-test" not in out
    assert "Claude Code" not in out  # no claude CLI on PATH


def test_doctor_ok_and_claude_code_registered(monkeypatch, capsys, student_config):
    FakeClaude(monkeypatch, registered=True)
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "OK")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "IMAP girişi       : OK" in out and "'posta' ekli" in out
    assert "Ne yapmalı" not in out


def test_doctor_claude_code_not_registered_is_a_hint_only(monkeypatch, capsys, student_config):
    FakeClaude(monkeypatch, registered=False)
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "OK")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "'posta' ekli değil" in out
    assert "→ Ne yapmalı: claude mcp add --scope user posta -- uvx posta-mcp serve" in out


def test_doctor_survives_claude_timeout(monkeypatch, capsys, student_config):
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/bin/claude")

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(setup_wizard.subprocess, "run", hang)
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "OK")
    assert main(["doctor"]) == 0
    assert "kontrol edilemedi" in capsys.readouterr().out
