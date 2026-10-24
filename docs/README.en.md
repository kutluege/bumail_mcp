# posta-mcp (English)

Türkçe: [../README.md](../README.md)

A local-first [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client)
read, search, organize and draft email in **your own mailbox over IMAP/SMTP**. It runs on your
machine, talks to your mail server directly, and has no cloud component of its own.

Boğaziçi University ("bumail") is the first built-in institution preset. It also ships Turkish
summary features for school mail: `hoca-ozet` (professors), `ogrenci-ozet` (students) and
`bana-ozet` (personalized, from your own interest profile). Other institutions or any
IMAP/SMTP provider work through the `generic` preset.

> **Status: alpha.** The Boğaziçi preset values come from public IT documentation and have **not**
> been verified against the live servers yet. See [Boğaziçi](#boğaziçi-university) and
> [docs/HANDOFF.md](HANDOFF.md).

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
- Provided **as-is, without warranty** (MIT, see [LICENSE](../LICENSE)).

## Safety model

These seven rules are enforced in code and covered by tests:

1. **Local stdio only.** Nothing listens on a network port. stdout carries only protocol messages;
   logs go to stderr.
2. **Your password lives only in the OS keychain or an environment variable** (which Claude
   Desktop and the Claude Code plugin fill from their own secure storage). Never in files,
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
[docs/CLIENTS.md](CLIENTS.md).

### Claude Desktop (easiest, no terminal)

Download `posta-mcp-<version>.mcpb` from the [GitHub releases](https://github.com/kutluege/bumail_mcp/releases)
and double-click it (or drop it on Claude Desktop's Settings > Extensions). The install dialog (in
Turkish) asks only for your Boğaziçi email address, your password (stored by Desktop as a secret)
and whether to allow sending (leave it off). The bundle is Boğaziçi-only; other IMAP providers use
Claude Code. Then type `/posta:baslangic` in a new chat. If the extension does not start, install
uv (below) and restart Desktop. Sending probably will not work in Desktop; drafts do. See
[docs/CLIENTS.md](CLIENTS.md).

posta-mcp does not work on the claude.ai website or in the Claude mobile apps: it is a local
(stdio-only) server. In Cowork it works through the plugin below.

### Claude Code and Cowork plugin

Needs [uv](https://docs.astral.sh/uv/) (install it as shown in the next section). The plugin works
in Claude Code and in Cowork tasks that run on your computer. It does not load in regular chat.

Once the plugin is listed in Anthropic's plugin directory:

1. In Claude Desktop, open Settings > Plugins > Discover, find "Boğaziçi Postanesi" and add it.
   (Or type `/plugin` in Claude Code and install it from the Discover tab; a plugin you add in
   Desktop reaches Claude Code by itself.)
2. Claude Code asks for your email address, your password, whether to allow sending (leave it
   off) and the preset. To change them later, type `/plugin` and pick the plugin on the Installed
   tab.
3. If the tools do not show up, type `/reload-plugins`. Then type `/`, search for `baslangic` and
   run it.

**In Cowork:** Cowork does not ask for the plugin's settings. After adding the plugin, run this
once in a terminal: `uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp setup`. It asks for your email address and
password, tests the login and saves them on this computer (the password in the OS keychain).
Answer "H" (no) when it offers to add the server to Claude Code; the plugin already does that.
Then start a new Cowork task and ask Claude to check your mail connection. If your password changes, run the
`parola` command the same way.

Before it is listed, load the plugin from a clone (also needs [git](https://git-scm.com/)):

```
git clone https://github.com/kutluege/bumail_mcp
claude --plugin-dir ./bumail_mcp
```

This loads it for that session only, so start Claude Code with `--plugin-dir` each time. If you are
not asked for your email address, type `/plugin` and fill in the plugin's settings. Installing by
name from the GitHub repository (`/plugin install ... --marketplace kutluege/bumail_mcp`) needs a
`.claude-plugin/marketplace.json`, which this repository does not have.

You can keep the password in the OS keychain instead of the plugin settings:
`uv run --directory ./bumail_mcp posta-mcp setup`. Then leave the plugin's password field empty;
the server reads the keychain when the field is empty. If `setup` offers to add the server to
Claude Code, answer no, or the same server shows up twice.

With the plugin, prompt and tool names are longer: the prompts are
`/mcp__plugin_posta-mcp_posta__hoca-ozet 7` and so on (type `/` and search for `hoca-ozet`), and
the tools are `mcp__plugin_posta-mcp_posta__<tool>`. Preset: `bogazici` for Boğaziçi accounts;
for another provider pick `generic` and save the server names first with `posta-mcp setup`.

To remove it, type `/plugin` and uninstall it on the Installed tab.

### Claude Code without the plugin (two commands)

Needs [git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/).

1. Install uv once:
   `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows) or
   `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux), then open a new terminal.
2. Run the setup wizard (prompts are in Turkish):

   ```
   uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp setup
   ```

   It asks for your email (and detects a Boğaziçi student/staff account from it), your password,
   tests the login *before* saving anything, retries on failure, stores the password in the OS
   keychain and finally offers to run `claude mcp add --scope user posta -- <serve command>` for
   you. The serve command matches how posta-mcp was installed (git, a source checkout, or PyPI
   once published).

Other commands use the same prefix (`uvx --from git+https://github.com/kutluege/bumail_mcp
posta-mcp <command>`): `parola` (alias `password`; update only the password), `doctor` (check
config, keychain, login and the Claude Code registration, with a fix for every failure), `forget`
(alias `kaldir`; remove the keychain entry, config and optionally the profile). The git install
follows the `main` branch.

### From source

```
git clone https://github.com/kutluege/bumail_mcp
cd bumail_mcp
uv sync
uv run posta-mcp --version
```

## Setup and configuration

```
posta-mcp setup
```

asks for your email (the preset is picked from its domain; otherwise it offers the `generic` preset
and asks for the hosts), the login name (a sensible default is derived from the preset) and your
password, and tests the login before saving. The password is read with `getpass` (not echoed) and stored only in the OS
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
| Info and read (read-only) | `sunucu_bilgisi`, `klasorleri_listele`, `postalari_ara`, `postayi_oku`, `eki_oku` |
| Organize | `postalari_isaretle`, `postalari_tasi`, `cope_at`, `arsivle` |
| Drafts (saved to Drafts, never sent) | `taslak_olustur`, `yanit_taslagi_olustur` |
| Send (disabled by default, confirmation required) | `posta_gonder`, `taslagi_gonder` |
| Boğaziçi summaries (read-only bundles) | `hoca_ozet`, `ogrenci_ozet`, `bana_ozet` |
| Interest profile | `profili_goster`, `profili_guncelle` |

Defaults: 20 results per search (max 100), message text truncated at 20,000 characters with a
notice. Summary tools never call a model; they select and package messages, and your client's
model writes the summary.

Attachments: `posta_gonder`, `taslak_olustur` and `yanit_taslagi_olustur` take `attachments`, a list of
absolute paths of files on your computer (at most 10 files, 18 MB in total). Hidden files and
anything inside a hidden folder (`.ssh`, `.env`, Windows `AppData`, ...) are refused, and so are
network paths (`\\server\share`). The send confirmation shows
each attachment's name, size and full path.

## Prompts

| Prompt | What it does |
|---|---|
| `hoca-ozet` | Summarize recent mail from professors / staff |
| `ogrenci-ozet` | Summarize recent mail from students |
| `bana-ozet` | Personalized digest: school news relevant to your profile |

Each takes `days` (default 7) and `language` (default Turkish). In Claude Code they appear as
slash commands, for example `/posta:hoca-ozet 7` or `/mcp__posta__bana-ozet 3`.
With the Claude Code plugin the names are `/mcp__plugin_posta-mcp_posta__hoca-ozet 7` and so on.

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
`sunucu_bilgisi` list the unverified fields. Part-time (`pt.`) and retired (`retired.`) accounts use
different hosts and currently need explicit `imap.host` / `smtp.host` settings. The university's
2026 Exchange upgrade may have changed capabilities (MOVE, SPECIAL-USE). The maintainer-side
checklist is in [docs/HANDOFF.md](HANDOFF.md) (`scripts/live_verify.py`).

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
`profile.example.toml` from the package, or ask the assistant to run `profili_guncelle`):

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
`[sending] enabled = true` in `config.toml`. With the Claude Code plugin, use the plugin's "allow sending"
setting instead: the plugin always passes `POSTA_ALLOW_SEND`, which overrides `config.toml`. Even then, `posta_gonder` and `taslagi_gonder` ask you to
confirm each message in the client (From, To, Cc, Bcc, subject, attachments, full body), and
send exactly what was shown. If your client cannot show such a confirmation (Claude Desktop may not
today), sending refuses and you can still create drafts and send them from your normal mail app.
Be aware that a client-side hook that auto-approves elicitation prompts would defeat the
confirmation; do not configure one for this server.

## What the plugin runs

Each time Claude Code starts, it runs
`uv run --frozen --no-dev --directory <plugin folder> posta-mcp serve`.

- On first start, uv downloads the Python packages pinned in `uv.lock` from PyPI (`pypi.org`,
  `files.pythonhosted.org`) and installs them into a `.venv` folder inside the plugin folder.
- If Python 3.12 is not installed, uv also downloads it from its own download source.
- After that the program talks only to Claude Code (over stdio) and to your mail servers (over
  TLS). It opens no port and connects nowhere else.
- If you run `posta-mcp setup` yourself, it can run `claude mcp add` when you say yes. The plugin
  never does this on its own.

## Privacy policy

This policy covers the posta-mcp program and its Claude Code plugin.

**Data collected.** The program works with:

- What you enter at setup: your email address, your password, the sending setting and the preset.
- What Claude asks to read from your mailbox: folder names, message headers and bodies, and the
  text of attachments. They are read on demand, when a tool runs.
- The interests you save for `bana-ozet`.

The developer collects nothing. There is no telemetry, no usage statistics and no advertising.

**Use and storage.** Everything stays on your computer.

- Your password: in Claude Code's secure storage (the macOS Keychain; on systems without a supported
  keychain, `~/.claude/.credentials.json`) if you typed it into the plugin settings, or in the OS keychain (service name `posta-mcp`) if you used `posta-mcp setup`.
  The program never writes it to a file or a log.
- Your email address and the other plugin settings: in Claude Code's settings file
  (`~/.claude/settings.json`).
- `config.toml` (settings without the password) and `profile.toml` (your interests): in your user
  config folder. macOS: `~/Library/Application Support/posta-mcp/`, Linux: `~/.config/posta-mcp/`,
  Windows: `%LOCALAPPDATA%\posta-mcp\posta-mcp\`. `posta-mcp doctor` prints the exact path.
- Messages are not saved to disk or written to logs. Changes such as read flags, moves and drafts
  are made on your mail server.

**Sharing with third parties.** Data goes only to:

- Your provider's IMAP/SMTP servers. For Boğaziçi: `imap.std.bogazici.edu.tr:993` and
  `smtp.std.bogazici.edu.tr:587` (students), `imap.bogazici.edu.tr:993` and
  `smtp.bogazici.edu.tr:587` (staff). With the `generic` preset, the servers you set. Your password
  is sent only to these servers, to log in.
- The AI model provider that your Claude client uses (Anthropic for Claude, or the provider your
  organization chose) receives the mail content that Claude reads. That provider's privacy policy
  applies there.
- On first start, uv downloads packages from PyPI (see above). No mail data is sent with it.

Nothing is sent anywhere else.

**Retention.** The developer runs no server, so nothing is kept there. Your local settings stay
until you delete them: uninstall the plugin with `/plugin` and run `posta-mcp forget` (removes the
keychain password, the config and, if you choose, the profile). Your mail stays on your mail
server. How long the AI model provider keeps data depends on its own policy.

**Contact.** Questions and problems: [GitHub Issues](https://github.com/kutluege/bumail_mcp/issues).

## Development and testing

```
uv sync
uv run pytest -m "not greenmail" -q
uv run ruff check . && uv run ruff format --check .
```

Integration tests run against a local GreenMail server with a throwaway certificate; see
[docs/TESTING.md](TESTING.md). Design notes: [docs/SPEC.md](SPEC.md),
[docs/DECISIONS.md](DECISIONS.md), [docs/RESEARCH.md](RESEARCH.md). Packaging and
release steps: [docs/HANDOFF.md](HANDOFF.md).

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

MIT. See [LICENSE](../LICENSE). Copyright (c) 2026 posta-mcp contributors.
