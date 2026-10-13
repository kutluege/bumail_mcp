# Testing

All commands run from the repo root with `uv` (never bare `python`/`pip`).

## Unit tests

```
uv sync
uv run pytest -m "not greenmail" -q     # everything except the GreenMail integration tests
uv run ruff check . && uv run ruff format --check .
```

Works the same on Windows (PowerShell or Git Bash), macOS and Linux.

## Integration tests (GreenMail)

Integration tests live in `tests/integration/` and are automatically marked `greenmail`. They talk
to a local [GreenMail](https://greenmail-mail-test.github.io/greenmail/) server over **IMAPS** and
**SMTPS** with a throwaway TLS certificate (SANs `dns:localhost`, `ip:127.0.0.1`). Nothing
generated is committed: the jar, keystore, CA PEM, pid file and log live in `.greenmail/`
(gitignored). All credentials are obvious fakes.

If GreenMail is unreachable the tests are **skipped**; with `POSTA_REQUIRE_GREENMAIL=1` (CI) they
**fail** instead.

### Jar mode (default when Docker is absent; Windows/macOS/Linux)

Needs a JDK (for `keytool`), **Java 11 or newer recommended**. `scripts/greenmail.py` picks the
newest JDK it finds (`--java`, `$GREENMAIL_JAVA`, `$JAVA_HOME`, `PATH`, standard install dirs). The
pinned GreenMail 2.1.14 standalone jar is downloaded from Maven Central into `.greenmail/` and
verified against a pinned SHA-256. On Java 8 the IMAPS/SMTPS ports still work, but GreenMail's REST
API thread dies (it needs Java 11 classes) and the script warns.

```
uv run python scripts/greenmail.py certs      # keystore + .greenmail/ca.pem (once)
uv run python scripts/greenmail.py start      # background; waits until ports are open
uv run pytest tests/integration -q -m greenmail
uv run python scripts/greenmail.py stop
```

Other subcommands: `status`, `capabilities` (prints the live IMAP CAPABILITY / SMTP EHLO lines),
`certs --force` (regenerate). Log: `.greenmail/greenmail.log`.

### Parallel workers: named instances

Several agents/workers on one machine must not share one GreenMail, because the per-test fixture
wipes every test user's mailbox. Give each worker its own **instance**: its own ports, pid file and
log, sharing the same keystore and `ca.pem` (generated once; concurrent `certs`/`start` calls are
serialised with a lock file and publish the files atomically).

Each parallel worker exports a distinct `POSTA_GREENMAIL_INSTANCE`, then runs `start --instance X`
before its tests and `stop --instance X` after:

```
export POSTA_GREENMAIL_INSTANCE=worker1        # PowerShell: $env:POSTA_GREENMAIL_INSTANCE = "worker1"
uv run python scripts/greenmail.py start --instance worker1
uv run pytest tests/integration -q -m greenmail   # fixtures read the env var for the ports
uv run python scripts/greenmail.py stop --instance worker1
```

`--instance` defaults to `$POSTA_GREENMAIL_INSTANCE`; empty means the default instance (3993/3465,
`greenmail.pid`, `greenmail.log`). Ports for a name are derived deterministically:
`base = 13000 + 3 * (crc32(name) % 2000)`, then IMAPS = base, SMTPS = base + 1, REST API = base + 2
(range 13000-18999, clear of the defaults). `status --instance X` prints them. Pid/log files are
`.greenmail/greenmail-X.pid` / `.log`; Docker container `posta-greenmail-X`. Names are `[A-Za-z0-9_-]`,
at most 40 chars. If two names hash to the same ports, `start` refuses (ports in use without that
instance's pid file); pick another name. `stop --instance X` only ever kills the pid recorded for X;
never kill `java` by image name, that takes down everyone's server.

Servers bind to `127.0.0.1` only (Docker: published on `127.0.0.1`).

### Docker mode (CI)

```
uv run python scripts/greenmail.py certs
uv run python scripts/greenmail.py start --mode docker   # greenmail/standalone:2.1.14
POSTA_REQUIRE_GREENMAIL=1 uv run pytest tests/integration -q -m greenmail
uv run python scripts/greenmail.py stop
```

The keystore directory is mounted read-only at `/certs` in the container (container name
`posta-greenmail`, ports published on `127.0.0.1` only).

### Server layout

| Item | Value |
|---|---|
| Host used by tests | `127.0.0.1` (`localhost` is also in the SANs but is slow on Windows: it tries `::1` first) |
| IMAPS | port 3993 (implicit TLS) |
| SMTPS | port 3465 (implicit TLS) |
| CA / certificate (PEM) | `.greenmail/ca.pem` (pass as `POSTA_CA_FILE`) |
| Folder separator | `.` |

Test users (login == full address; password is fake):

| Role | Login / address | Password |
|---|---|---|
| student | `ogrenci@std.bogazici.test` | `ogrenci-fake-pass` |
| faculty | `hoca@bogazici.test` | `hoca-fake-pass` |
| outsider | `diger@example.test` | `diger-fake-pass` |

Mail sent over SMTPS to one of these addresses lands in that user's INBOX.

### Fixtures (`tests/integration/conftest.py`)

- `greenmail` (session) -> `GreenmailInfo`: `host`, `imaps_port`, `smtps_port`, `ca_file`, `users`,
  `.student/.faculty/.outsider`, `.password(user)`, `.ssl_context()` (default verifying context + test CA).
- `imap_client(user)` -> logged-in `imapclient.IMAPClient` over verified IMAPS (logged out at teardown).
- `seed_message(user, raw_bytes, folder="INBOX", flags=())` -> UID (IMAP APPEND; creates the folder).
- `smtp_send(from_user, msg, to=None)` -> authenticated send over verified SMTPS.
- `posta_env(user=None, **overrides)` -> dict of `POSTA_PRESET=generic`, `POSTA_EMAIL`,
  `POSTA_USERNAME`, `POSTA_PASSWORD`, `POSTA_IMAP_HOST/PORT/SECURITY`, `POSTA_SMTP_HOST/PORT/SECURITY`,
  `POSTA_CA_FILE` pointing posta-mcp at GreenMail (`None` override drops a key).
- Autouse, per test: every test user's messages and non-INBOX folders are deleted (plain EXPUNGE is
  fine here: this is test-server setup, not product code) and `POSTA_CA_FILE` is set.

## GreenMail 2.1.14 capabilities (verified against the running server)

IMAP (`CAPABILITY` after TLS, no login needed):

```
IMAP4rev1 LITERAL+ UIDPLUS SORT IDLE MOVE SASL-IR AUTH=XOAUTH2 QUOTA
```

| Capability | Available | Notes for product code |
|---|---|---|
| `MOVE` (RFC 6851) | yes | `UID MOVE` works (`imapclient.move`). Use it first. |
| `UIDPLUS` | yes | `UID COPY` + `\Deleted` + `UID EXPUNGE <uids>` removes only the targeted UIDs (tested: an untargeted `\Deleted` message survives). `APPENDUID` is returned by APPEND. |
| `IDLE` | yes | |
| `SPECIAL-USE` | **no** | Folders have no `\Trash`/`\Archive` attributes; resolve the trash folder by configured name. Only `INBOX` exists initially, so tests must create `Trash` etc. |
| `STARTTLS` (IMAP) | **no** | Only implicit-TLS IMAPS can be tested. |
| `STARTTLS` (SMTP) | **no** | SMTP EHLO offers only `AUTH PLAIN LOGIN XOAUTH2` and `SMTPUTF8` (no SIZE, 8BITMIME, STARTTLS). Only SMTPS can be tested. |
| `UNSELECT`, `NAMESPACE`, `CONDSTORE`, `ENABLE`, `LIST-EXTENDED`, `ID` | no | `imapclient.unselect_folder()` / `namespace()` raise `CapabilityError`; don't rely on them. |

The `starttls` connection mode therefore cannot be integration-tested against GreenMail and needs
a unit-level test with a fake/mocked server.

## TLS tests

`tests/integration/test_tls_verification.py` covers non-negotiable 3 against GreenMail: a default
context without the test CA fails with `ssl.SSLCertVerificationError` for IMAPS and SMTPS; with
the CA it succeeds; a name not in the certificate's SANs fails the hostname check even though the
CA is trusted. It also checks `posta_mcp.tls.make_ssl_context` once that module exists
(skipped until then).

## CI

`.github/workflows/ci.yml`: `lint` (ruff), `unit` (ubuntu, windows, macos), `integration`
(ubuntu, Docker GreenMail, `POSTA_REQUIRE_GREENMAIL=1`). Actions are pinned by commit SHA.
