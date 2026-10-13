import pytest

from posta_mcp.config import ConfigError, load_config, preview_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def bogazici(tmp_path, email, **extra_env):
    env = {"POSTA_PRESET": "bogazici", "POSTA_EMAIL": email, **extra_env}
    return load_config(env=env, path=tmp_path / "none.toml")


def test_bogazici_student_variant(tmp_path):
    cfg = bogazici(tmp_path, "ada.lovelace@std.bogazici.edu.tr")
    assert cfg.variant == "student"
    assert (cfg.imap.host, cfg.imap.port, cfg.imap.security) == (
        "imap.std.bogazici.edu.tr",
        993,
        "ssl",
    )
    assert (cfg.smtp.host, cfg.smtp.port, cfg.smtp.security) == (
        "smtp.std.bogazici.edu.tr",
        587,
        "starttls",
    )
    assert cfg.login == "ada.lovelace"  # localpart style
    assert {"imap.host", "smtp.host", "folders.sent", "domains.student"} <= set(cfg.unverified)
    assert "verified" not in " ".join(cfg.unverified)


@pytest.mark.parametrize("domain", ["bogazici.edu.tr", "boun.edu.tr", "BOGAZICI.edu.tr"])
def test_bogazici_staff_variant(tmp_path, domain):
    cfg = bogazici(tmp_path, f"ali.veli@{domain}")
    assert cfg.variant == "staff"
    assert (cfg.imap.host, cfg.smtp.host) == ("imap.bogazici.edu.tr", "smtp.bogazici.edu.tr")
    assert cfg.smtp.security == "starttls"


def test_bogazici_classification_and_domains(tmp_path):
    cfg = bogazici(tmp_path, "a.b@bogazici.edu.tr")
    assert {"pt.bogazici.edu.tr", "retired.bogazici.edu.tr", "alum.bogazici.edu.tr"} <= set(
        cfg.domains.institution
    )
    assert cfg.domains.student == ["std.bogazici.edu.tr"]
    assert "instforum@list.bogazici.edu.tr" in cfg.classification.list_addresses
    assert cfg.classification.list_domains == ["list.bogazici.edu.tr"]
    assert "mailer-daemon" in cfg.classification.standard_local_parts


def test_bogazici_unknown_domain_errors_naming_expected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        bogazici(tmp_path, "someone@gmail.com")
    assert "std.bogazici.edu.tr" in str(exc.value) and "boun.edu.tr" in str(exc.value)


def test_bogazici_unknown_domain_ok_with_explicit_hosts(tmp_path):
    cfg = bogazici(
        tmp_path, "x@pt.bogazici.edu.tr", POSTA_IMAP_HOST="imap.example", POSTA_SMTP_HOST="smtp.ex"
    )
    assert cfg.variant == "" and cfg.imap.host == "imap.example"
    assert cfg.unverified == [
        f"{s}.{f}"
        for s in ("folders", "domains", "classification")
        for f in type(getattr(cfg, s)).model_fields
        if f != "verified"
    ]


def test_username_override_wins(tmp_path):
    assert bogazici(tmp_path, "a.b@bogazici.edu.tr", POSTA_USERNAME="other").login == "other"
    path = write(tmp_path, 'username = "fromfile"\n')
    env = {"POSTA_PRESET": "bogazici", "POSTA_EMAIL": "a.b@bogazici.edu.tr"}
    assert load_config(env=env, path=path).login == "fromfile"


def test_generic_login_is_full_email(tmp_path):
    env = {"POSTA_EMAIL": "me@example.org"}
    assert load_config(env=env, path=tmp_path / "x.toml").login == "me@example.org"


def test_empty_env_values_count_as_unset(tmp_path):
    env = {k: "" for k in ("POSTA_PRESET", "POSTA_EMAIL", "POSTA_IMAP_HOST", "POSTA_IMAP_PORT")}
    env |= {"POSTA_ALLOW_SEND": "  ", "POSTA_CONFIG": "", "POSTA_CA_FILE": ""}
    cfg = load_config(env=env, path=tmp_path / "x.toml")
    assert (cfg.preset, cfg.email, cfg.imap.host, cfg.imap.port) == ("generic", "", "", 993)
    assert cfg.send_enabled is False and cfg.ca_file is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("TRUE", True), ("Yes", True), ("on", True),
     ("0", False), ("False", False), ("NO", False), ("Off", False)],
)  # fmt: skip
def test_bool_env_lenient(tmp_path, value, expected):
    cfg = load_config(env={"POSTA_ALLOW_SEND": value}, path=tmp_path / "x.toml")
    assert cfg.send_enabled is expected


def test_bool_env_garbage_is_error(tmp_path):
    with pytest.raises(ConfigError, match="POSTA_ALLOW_SEND"):
        load_config(env={"POSTA_ALLOW_SEND": "maybe"}, path=tmp_path / "x.toml")


def test_preview_config(tmp_path):
    cfg = preview_config("bogazici", "q@std.bogazici.edu.tr")
    assert cfg.variant == "student" and cfg.login == "q"


def test_generic_needs_hosts(tmp_path):
    cfg = load_config(env={}, path=tmp_path / "missing.toml")
    assert cfg.preset == "generic"
    assert cfg.missing() == ["email", "imap.host", "smtp.host"]
    assert cfg.unverified == []


def test_precedence_preset_then_file_then_env(tmp_path):
    path = write(
        tmp_path,
        'preset = "bogazici"\nemail = "a@std.bogazici.edu.tr"\n'
        '[imap]\nhost = "file.example.org"\nport = 143\nsecurity = "starttls"\n',
    )
    cfg = load_config(env={}, path=path)
    assert (cfg.imap.host, cfg.imap.port, cfg.imap.security) == (
        "file.example.org",
        143,
        "starttls",
    )
    assert cfg.smtp.host == "smtp.std.bogazici.edu.tr"  # untouched variant value

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
