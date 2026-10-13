<!-- mcp-name: io.github.OWNER/posta-mcp -->

# posta-mcp

A local-first [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client)
read, search, organize and draft email in **your own mailbox over IMAP/SMTP**. It runs on your
machine, talks to your mail server directly, and has no cloud component of its own.

Boğaziçi University ("bumail") is the first built-in institution preset. It also ships Turkish
summary features for school mail: `hoca-ozet` (professors), `ogrenci-ozet` (students) and
`bana-ozet` (personalized, from your own interest profile). Other institutions or any
IMAP/SMTP provider work through the `generic` preset.

> **Status: alpha.** The Boğaziçi preset values come from public IT documentation and have **not**
> been verified against the live servers yet. See [Boğaziçi](#boğaziçi-university) and
> [docs/HANDOFF.md](docs/HANDOFF.md).

## Important notes (please read)

- **Unofficial.** Not affiliated with or endorsed by Boğaziçi University or any other institution.
- **Check your institution's IT / acceptable-use policy** before connecting third-party tools to
  your university account.
- **Your email content goes to the model provider** your MCP client uses (for example Anthropic when
  you use Claude). Anything the model reads through these tools leaves your machine that way.
- **Models can misread or mis-summarize.** Verify important information (deadlines, grades,
  exam dates) in the original message.
- **Sending is off by default and always requires your confirmation.** You are responsible for what
  you send.
- **Preset values marked `unverified` have not been tested against the live server.**
- Provided **as-is, without warranty** (MIT, see [LICENSE](LICENSE)).

## Safety model

These seven rules are enforced in code and covered by tests:

1. **Local stdio only.** Nothing listens on a network port. stdout carries only protocol messages;
   logs go to stderr.
2. **Your password lives only in the OS keychain or an environment variable.** Never in files,
   logs, tool output, error messages, or test data.
3. **TLS is always verified.** There is no "skip verification" switch. Plaintext IMAP/SMTP is not
   supported (only implicit TLS or STARTTLS). `POSTA_CA_FILE` can only *add* a trusted CA for
   testing; it never turns checks off.
4. **Sending is off by default.** Reading and drafting work out of the box. Send tools refuse until
   you enable sending yourself, and every send needs your confirmation through the client
   (MCP elicitation) showing the exact message. If the client cannot ask you, it refuses and
   suggests a draft. No tool can turn sending on.
5. **No permanent delete.** "Delete" means move to Trash. The server never issues a plain `EXPUNGE`
   and never closes a folder in a way that expunges; moves use `UID MOVE`, or a targeted
   `UID COPY` + `\Deleted` + `UID EXPUNGE` of exactly the chosen messages, and refuse if the
   server supports neither.
6. **UIDs and PEEK.** Every message operation uses UIDs, and message bodies are fetched with
   `BODY.PEEK`, so reading never marks mail as read unless you explicitly ask.
7. **Email content is untrusted.** Message text is wrapped in clearly marked delimiters, the model is
   told it is data and not instructions, and bodies are never written to logs.

## Install

You need an MCP client (Claude Code, Claude Desktop, MCP Inspector, ...). Details per client are in
[docs/CLIENTS.md](docs/CLIENTS.md).

### uvx (recommended)

Needs [uv](https://docs.astral.sh/uv/). After the first release on PyPI:

```
uvx posta-mcp setup        # one-time: stores your password in the OS keychain
uvx posta-mcp doctor       # checks config, keychain and IMAP login
```

Run the server from a client, for example with Claude Code:

```
claude mcp add --transport stdio --env POSTA_EMAIL=ad.soyad@std.bogazici.edu.tr posta -- uvx posta-mcp serve
```

Before the PyPI release you can use a built wheel: `uv build`, then
`uvx --from dist/posta_mcp-0.1.0-py3-none-any.whl posta-mcp --version`.

### Claude Desktop (MCPB bundle)

Download `posta-mcp-<version>.mcpb` from the GitHub release and open it in Claude Desktop
(Settings > Extensions). It asks for your institution preset, email address, password (stored by
the host as a secret) and whether to allow sending. See [docs/CLIENTS.md](docs/CLIENTS.md) for
the Desktop caveats (notably: sending probably will not work there, drafts do).

### From source

```
git clone https://github.com/OWNER/posta-mcp
cd posta-mcp
uv sync
uv run posta-mcp --version
```

## Setup and configuration

```
posta-mcp setup
```

asks for the preset, your email, the login name (a sensible default is derived from the preset) and
your password. The password is read with `getpass` (not echoed) and stored only in the OS
keychain (service `posta-mcp`, key = login name). Non-secret settings go to `config.toml` in your
user config directory (`posta-mcp doctor` prints the path). Then it tests the IMAP login.

Alternatively set environment variables (for example in your client's MCP config) and skip the
config file. Environment values override the file; empty values count as unset.

| Variable | Meaning |
|---|---|
| `POSTA_CONFIG` | path to `config.toml` (default: user config dir) |
| `POSTA_PRESET` | `bogazici` or `generic` |
| `POSTA_EMAIL` | your full email address |
| `POSTA_USERNAME` | login name if different from the preset default |
| `POSTA_PASSWORD` | password (otherwise read from the keychain) |
| `POSTA_IMAP_HOST` / `_PORT` / `_SECURITY` | IMAP server; security is `ssl` (implicit TLS) or `starttls` |
| `POSTA_SMTP_HOST` / `_PORT` / `_SECURITY` | SMTP server, same values |
| `POSTA_ALLOW_SEND` | `true` to allow sending (still confirmed per message); `0/false/no/off` forces it off |
| `POSTA_PROFILE` | path to the `bana-ozet` interest profile (default: `profile.toml` beside `config.toml`) |
| `POSTA_LOG_LEVEL` | logging level (stderr only) |
| `POSTA_CA_FILE` | extra trusted CA, for local test servers only |

Example `config.toml` (never put a password in it; the server rejects such keys):

```toml
preset = "generic"
email = "me@example.org"

[imap]
host = "imap.example.org"

[smtp]
host = "smtp.example.org"
port = 587
security = "starttls"

[sending]
enabled = false      # see "Enabling sending"
```

Special folders are found through IMAP SPECIAL-USE first, then the preset's folder names, then your
`[folders]` overrides.

## Tools

| Group | Tools |
|---|---|
| Info and read (read-only) | `server_info`, `list_folders`, `search_messages`, `get_message`, `get_attachment_text` |
| Organize | `mark_messages`, `move_messages`, `trash_messages`, `archive_messages` |
| Drafts (saved to Drafts, never sent) | `create_draft`, `create_reply_draft` |
| Send (disabled by default, confirmation required) | `send_message`, `send_draft` |
| Boğaziçi summaries (read-only bundles) | `hoca_ozet`, `ogrenci_ozet`, `bana_ozet` |
| Interest profile | `get_profile`, `update_profile` |

Defaults: 20 results per search (max 100), message text truncated at 20,000 characters with a
notice. Summary tools never call a model; they select and package messages, and your client's
model writes the summary.

## Prompts

| Prompt | What it does |
|---|---|
| `hoca-ozet` | Summarize recent mail from professors / staff |
| `ogrenci-ozet` | Summarize recent mail from students |
| `bana-ozet` | Personalized digest: school news relevant to your profile |

Each takes `days` (default 7) and `language` (default Turkish). In Claude Code they appear as
slash commands, for example `/posta:hoca-ozet 7` or `/mcp__posta__bana-ozet 3`.

## Boğaziçi University

Choose the `bogazici` preset. The preset picks the hosts **from your email domain**:

| Account | Email domain | IMAP (993, TLS) | SMTP (587, STARTTLS) |
|---|---|---|---|
| Student | `@std.bogazici.edu.tr` | `imap.std.bogazici.edu.tr` | `smtp.std.bogazici.edu.tr` |
| Staff / faculty | `@bogazici.edu.tr` (and legacy `@boun.edu.tr`) | `imap.bogazici.edu.tr` | `smtp.bogazici.edu.tr` |

The login name is the **local part** of your address (`ad.soyad`, not `ad.soyad@...`). The preset
fills this in automatically.

**Everything above is UNVERIFIED.** Hosts, ports, security, the login-name rule, folder names,
institutional/student domains and the mailing-list / automated-sender rules were taken from public
IT documentation and DNS and have not been tested against the live server. `posta-mcp doctor` and
`server_info` list the unverified fields. Part-time (`pt.`) and retired (`retired.`) accounts use
different hosts and currently need explicit `imap.host` / `smtp.host` settings. The university's
2026 Exchange upgrade may have changed capabilities (MOVE, SPECIAL-USE). The maintainer-side
checklist is in [docs/HANDOFF.md](docs/HANDOFF.md) (`scripts/live_verify.py`).

### hoca-ozet, ogrenci-ozet, bana-ozet

Mail is sorted into categories using headers (List-Id, Auto-Submitted, ...) and the preset's rules:
`list` (mailing lists), `standard` (automated / institutional addresses), `student`, `faculty`
(an institutional sender that is none of the others) and `external`.

- **hoca-ozet**: recent mail from likely professors and staff.
- **ogrenci-ozet**: recent mail from student addresses.
- **bana-ozet**: reads your profile, picks recent institutional and list mail that matches your
  interests (case, Turkish diacritics and Turkish suffixes are handled: `İ/i`, `I/ı`,
  `kampüsünde`), and also gives a short subject-only list of the other recent school mail.

Your interest profile is a small non-secret file, `profile.toml`, next to `config.toml` (copy
`profile.example.toml` from the package, or ask the assistant to run `update_profile`):

```toml
notes = "Mostly on Güney Kampüs; eat at the yemekhane most days; play tennis."

[[interests]]
name = "Güney Kampüs"
keywords = ["güney kampüs", "south campus", "güney yerleşke"]

[[interests]]
name = "Yemekhane"
keywords = ["yemekhane", "cafeteria", "yemek listesi", "menü"]

[[interests]]
name = "Tenis"
keywords = ["tenis", "tennis", "tenis kortu", "kort"]
```

Remember: these tools hand the selected message text to your model provider, and summaries can be
wrong. Check deadlines and announcements in the original mail.

## Enabling sending

Off by default. To allow it, set `POSTA_ALLOW_SEND=true` in the client's environment or
`[sending] enabled = true` in `config.toml`. Even then, `send_message` and `send_draft` ask you to
confirm each message in the client (From, To, Cc, Bcc, subject, attachments, full body), and
send exactly what was shown. If your client cannot show such a confirmation (Claude Desktop may not
today), sending refuses and you can still create drafts and send them from your normal mail app.
Be aware that a client-side hook that auto-approves elicitation prompts would defeat the
confirmation; do not configure one for this server.

## Development and testing

```
uv sync
uv run pytest -m "not greenmail" -q
uv run ruff check . && uv run ruff format --check .
```

Integration tests run against a local GreenMail server with a throwaway certificate; see
[docs/TESTING.md](docs/TESTING.md). Design notes: [docs/SPEC.md](docs/SPEC.md),
[docs/DECISIONS.md](docs/DECISIONS.md), [docs/RESEARCH.md](docs/RESEARCH.md). Packaging and
release steps: [docs/HANDOFF.md](docs/HANDOFF.md).

## Türkçe özet

**posta-mcp**, e-postanızı IMAP/SMTP üzerinden Claude gibi MCP istemcilerine açan, tamamen
bilgisayarınızda çalışan bir sunucudur. İlk hazır ayar Boğaziçi Üniversitesi (bumail) içindir.

- **Resmi değildir**; Boğaziçi Üniversitesi veya başka bir kurumla bağlantısı yoktur, onlar
  tarafından onaylanmamıştır. Üçüncü taraf araçları bağlamadan önce kurumunuzun BT / kabul
  edilebilir kullanım politikasını kontrol edin.
- Okuduğunuz e-posta içeriği, kullandığınız istemcinin model sağlayıcısına gönderilir. Modeller
  yanlış okuyabilir veya yanlış özetleyebilir; son tarihleri ve notları mutlaka kontrol edin.
- Parolanız yalnızca işletim sistemi anahtar zincirinde (`posta-mcp setup`) veya ortam
  değişkeninde tutulur. TLS doğrulaması her zaman açıktır.
- **Gönderme varsayılan olarak kapalıdır** ve açık olsa bile her gönderim sizin onayınızı ister.
  Silme işlemi kalıcı değildir (Çöp Kutusu'na taşır). Sorumluluk size aittir; yazılım olduğu gibi,
  garanti vermeden sunulur.
- Boğaziçi ayarları (sunucu adları, portlar, kullanıcı adı kuralı, klasör adları, liste kuralları)
  **henüz canlı sunucuda doğrulanmamıştır** (`unverified`). Kullanıcı adı, e-posta adresinin `@`
  öncesi kısmıdır (`ad.soyad`).
- Özet özellikleri: `/hoca-ozet` (hocalardan gelen postalar), `/ogrenci-ozet` (öğrencilerden gelenler),
  `/bana-ozet` (ilgi alanlarınıza göre: Güney Kampüs, yemekhane, tenis gibi). Varsayılan çıktı
  dili Türkçedir.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 posta-mcp contributors.
