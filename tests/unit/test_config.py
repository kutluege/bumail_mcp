import pytest

from posta_mcp.config import ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_preset_only_bogazici_is_unverified(tmp_path):
    cfg = load_config(env={"POSTA_PRESET": "bogazici"}, path=tmp_path / "none.toml")
    assert cfg.preset == "bogazici"
    assert "std.bogazici.edu.tr" in cfg.domains.student
    assert "students@list.bogazici.edu.tr" in cfg.classification.list_addresses
    assert {"imap.host", "smtp.host", "folders.sent", "domains.student"} <= set(cfg.unverified)
    assert "verified" not in " ".join(cfg.unverified)


def test_generic_needs_hosts(tmp_path):
    cfg = load_config(env={}, path=tmp_path / "missing.toml")
    assert cfg.preset == "generic"
    assert cfg.missing() == ["email", "imap.host", "smtp.host"]
    assert cfg.unverified == []


def test_precedence_preset_then_file_then_env(tmp_path):
    path = write(
        tmp_path,
        'preset = "bogazici"\nemail = "a@std.boun.edu.tr"\n'
        '[imap]\nhost = "file.example.org"\nport = 143\nsecurity = "starttls"\n',
    )
    cfg = load_config(env={}, path=path)
    assert (cfg.imap.host, cfg.imap.port, cfg.imap.security) == (
        "file.example.org",
        143,
        "starttls",
    )
    assert cfg.smtp.host == "mail.boun.edu.tr"  # untouched preset value
    assert cfg.login == "a@std.boun.edu.tr"  # username defaults to email

    env = {
        "POSTA_IMAP_HOST": "env.example.org",
        "POSTA_IMAP_PORT": "1993",
        "POSTA_IMAP_SECURITY": "SSL",
        "POSTA_USERNAME": "user1",
        "POSTA_PRESET": "generic",
    }
    cfg = load_config(env=env, path=path)
    assert cfg.preset == "generic"
    assert (cfg.imap.host, cfg.imap.port, cfg.imap.security) == ("env.example.org", 1993, "ssl")
    assert cfg.login == "user1"


@pytest.mark.parametrize("bad", ["none", "plain", "tls", ""])
def test_invalid_security_rejected(tmp_path, bad):
    path = write(tmp_path, f'[imap]\nhost = "h"\nsecurity = "{bad}"\n')
    with pytest.raises(ConfigError):
        load_config(env={}, path=path)


def test_invalid_security_from_env_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env={"POSTA_SMTP_SECURITY": "none"}, path=tmp_path / "x.toml")


def test_sending_disabled_by_default_and_env_enables(tmp_path):
    path = tmp_path / "x.toml"
    assert load_config(env={}, path=path).send_enabled is False
    assert load_config(env={"POSTA_ALLOW_SEND": "1"}, path=path).send_enabled is True
    assert load_config(env={"POSTA_ALLOW_SEND": "false"}, path=path).send_enabled is False
    on = write(tmp_path, "[sending]\nenabled = true\n")
    assert load_config(env={}, path=on).send_enabled is True
    assert load_config(env={"POSTA_ALLOW_SEND": "0"}, path=on).send_enabled is False


def test_password_in_file_rejected_without_echo(tmp_path):
    path = write(tmp_path, 'password = "hunter2-secret"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(env={}, path=path)
    assert "hunter2" not in str(exc.value)


def test_unknown_preset_and_bad_port(tmp_path):
    with pytest.raises(ConfigError):
        load_config(env={"POSTA_PRESET": "../etc"}, path=tmp_path / "x.toml")
    with pytest.raises(ConfigError):
        load_config(env={"POSTA_IMAP_PORT": "abc"}, path=tmp_path / "x.toml")


def test_posta_config_env_selects_file(tmp_path):
    path = write(tmp_path, 'email = "me@example.org"\n')
    assert load_config(env={"POSTA_CONFIG": str(path)}).email == "me@example.org"
