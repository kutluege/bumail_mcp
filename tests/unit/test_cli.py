import pytest

from posta_mcp import __version__
from posta_mcp.cli import _write_config, main
from posta_mcp.config import load_config


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_refuses_without_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POSTA_CONFIG", str(tmp_path / "none.toml"))
    for name in ("POSTA_EMAIL", "POSTA_PRESET", "POSTA_IMAP_HOST", "POSTA_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert main(["serve"]) == 2
    captured = capsys.readouterr()
    assert "missing settings" in captured.err
    assert captured.out == ""


def test_written_config_has_no_secret_and_round_trips(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    _write_config(path, "generic", "ü@example.org", "login", {"imap": "i.example.org"})
    text = path.read_text(encoding="utf-8")
    assert "password" not in text.lower()
    cfg = load_config(env={}, path=path)
    assert (cfg.email, cfg.username, cfg.imap.host) == ("ü@example.org", "login", "i.example.org")


def test_doctor_reports_without_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POSTA_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("POSTA_PRESET", "bogazici")
    monkeypatch.setenv("POSTA_EMAIL", "x.y@std.bogazici.edu.tr")
    monkeypatch.setenv("POSTA_PASSWORD", "pw-doctor-test")
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "FAILED (stub)")
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "unverified" in out and "imap.host" in out
    assert "student" in out and "login       : x.y" in out
    assert "pw-doctor-test" not in out


def test_setup_derives_variant_and_shows_hosts_before_password(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("POSTA_CONFIG", str(path))
    for name in ("POSTA_EMAIL", "POSTA_PRESET", "POSTA_USERNAME", "POSTA_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    answers = iter(["bogazici", "ada.lovelace@std.bogazici.edu.tr", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    seen_before_password: list[str] = []

    def fake_getpass(_prompt=""):
        seen_before_password.append(capsys.readouterr().out)
        return "pw-setup-test"

    stored: dict[str, str] = {}
    monkeypatch.setattr("getpass.getpass", fake_getpass)
    monkeypatch.setattr("posta_mcp.cli.set_password", lambda u, p: stored.update({u: p}))
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "OK")
    assert main(["setup"]) == 0
    assert "imap.std.bogazici.edu.tr:993" in seen_before_password[0]
    assert "smtp.std.bogazici.edu.tr:587 (starttls)" in seen_before_password[0]
    assert stored == {"ada.lovelace": "pw-setup-test"}  # localpart login
    text = path.read_text(encoding="utf-8")
    assert "username" not in text and "pw-setup-test" not in text


def test_setup_rejects_unmatched_domain(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POSTA_CONFIG", str(tmp_path / "config.toml"))
    answers = iter(["bogazici", "me@gmail.com"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert main(["setup"]) == 2
    assert "std.bogazici.edu.tr" in capsys.readouterr().err
