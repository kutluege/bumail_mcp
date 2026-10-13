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
    monkeypatch.setenv("POSTA_EMAIL", "x@std.boun.edu.tr")
    monkeypatch.setenv("POSTA_PASSWORD", "pw-doctor-test")
    monkeypatch.setattr("posta_mcp.cli.try_imap_login", lambda c, p: "FAILED (stub)")
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "unverified" in out and "imap.host" in out
    assert "pw-doctor-test" not in out
