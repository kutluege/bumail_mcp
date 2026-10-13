# posta-mcp — Implementation Spec

posta-mcp is a local-first MCP server that gives Claude (and other MCP clients) access to a
user's mailbox over IMAP/SMTP. Boğaziçi University ("bumail") is the first institution preset;
everything institution-specific lives in a preset file so other institutions can be added.

This spec is a starting point. Where installed libraries or current docs disagree with it,
follow reality and record the deviation in `docs/DECISIONS.md` (one or two lines each).

## 1. Non-negotiables

1. **stdio transport only.** Nothing listens on a port. stdout carries protocol messages only;
   all logging goes to stderr.
2. **Credentials** only in the OS keychain (`keyring`) or environment variables. Never in files,
   logs, tool outputs, exceptions surfaced to the client, or test fixtures.
3. **TLS verification always on.** No insecure/skip-verify option anywhere. A custom CA file
   (`POSTA_CA_FILE`) may be *added* for tests; it never disables verification or hostname checks.
   Plaintext IMAP/SMTP is not supported; connection security is `ssl` (implicit TLS) or `starttls`.
4. **Sending is off by default.** Read and draft tools work out of the box. Send tools refuse
   until the user enables sending in config/env, and every real send needs a human confirmation
   through MCP elicitation. If the client cannot elicit, sending refuses (suggest a draft instead).
   No tool may enable sending.
5. **No permanent delete.** "Delete" means move to Trash. Never issue a plain `EXPUNGE`. Moves
   use `UID MOVE` (RFC 6851); fallback is `UID COPY` + `\Deleted` + `UID EXPUNGE <uids>` (UIDPLUS)
   on exactly the targeted UIDs; if neither is available, refuse.
6. **UIDs** for every message operation. Bodies are fetched with `BODY.PEEK[...]`; reading never
   sets `\Seen` unless the caller explicitly asks (`mark_seen=true`).
7. **Email content is untrusted.** Wrap it in clear delimiters in tool output, tell the model it
   is data not instructions, and never log message bodies (log UIDs/counts only).

Each non-negotiable has at least one test.

## 2. Stack

- Python ≥ 3.11, managed with `uv`; build backend `hatchling`; src layout.
- `mcp` (official Python SDK, currently 2.x — verify the API in the installed version, especially
  the high-level server class, `Context`, elicitation, tool annotations, prompts).
- `imapclient` for IMAP (UID mode by default), stdlib `smtplib` for SMTP, stdlib `email` with
  `policy.default` for parsing/composing.
- `keyring` (credentials), `platformdirs` (config dir), `pydantic` (schemas), stdlib `tomllib`.
- Dev: `pytest`, `ruff`. Sync IMAP calls may run in a worker thread from async tools.

## 3. Layout and module contracts

```
src/posta_mcp/
  __init__.py        __version__
  __main__.py        python -m posta_mcp
  cli.py             posta-mcp [serve] | setup | doctor | --version
  logs.py            stderr-only logging setup, redaction helper
  config.py          Config model; load preset + user config.toml + env overrides
  presets/*.toml     bogazici.toml, generic.toml (package data)
  credentials.py     get_password(account) from env POSTA_PASSWORD or keyring; set via setup
  tls.py             make_ssl_context(ca_file=None) -> ssl.SSLContext (verify always on)
  mime.py            parse raw bytes -> ParsedMessage; header/charset decoding; html->text
  untrusted.py       wrap_untrusted(text, **attrs) delimiting + neutralising fake end tags
  compose.py         build EmailMessage for new mail / reply (UTF-8, RFC 2047 headers, threading)
  imap.py            Mailbox: connect, list_folders, search, fetch, flags, move, trash, append
  smtp.py            send(EmailMessage) over ssl/starttls with verified TLS
  classify.py        sender category from headers + preset rules
  profile.py         user interest profile store (bana-ozet)
  digest.py          candidate selection for hoca/ogrenci/bana summaries
  server.py          build_server(config) -> MCP server; registers tool modules; run stdio
  tools/read.py      list_folders, search_messages, get_message, get_attachment_text (optional)
  tools/organize.py  mark_messages, move_messages, trash_messages, archive_messages
  tools/drafts.py    create_draft, create_reply_draft
  tools/send.py      send_message, send_draft (gated)
  tools/digest.py    hoca_ozet, ogrenci_ozet, bana_ozet, get_profile, update_profile
  prompts.py         MCP prompts: hoca-ozet, ogrenci-ozet, bana-ozet
```

Each `tools/*.py` exposes `register(server, deps)` so modules can be built in parallel. `deps`
gives access to config and a mailbox factory/context manager.

Key internal types (adjust names if needed, keep the shape):

- `MessageSummary`: uid, folder, from_name, from_addr, to, cc, subject, date (ISO), flags,
  size, has_attachments, list_id, message_id.
- `ParsedMessage`: summary fields + text body, html-derived text, attachments
  (filename, content_type, size, index), headers needed for threading
  (Message-ID, In-Reply-To, References, List-Id, Reply-To).
- `Mailbox.search(folder, criteria, limit) -> list[MessageSummary]` (newest first),
  `Mailbox.fetch(folder, uid) -> ParsedMessage` (PEEK), `Mailbox.set_flags`, `Mailbox.move`,
  `Mailbox.trash`, `Mailbox.append(folder, msg_bytes, flags)`.

## 4. Configuration

- File: `$POSTA_CONFIG` or `platformdirs.user_config_dir("posta-mcp")/config.toml`. Non-secret only.
- Keys: `preset`, `email`, `username` (defaults to email), optional overrides for `imap`, `smtp`,
  `folders`, `[sending] enabled=false`, `save_sent = "auto"|"always"|"never"`.
- Env overrides: `POSTA_PRESET`, `POSTA_EMAIL`, `POSTA_USERNAME`, `POSTA_PASSWORD`,
  `POSTA_IMAP_HOST/PORT/SECURITY`, `POSTA_SMTP_HOST/PORT/SECURITY`, `POSTA_CA_FILE`,
  `POSTA_ALLOW_SEND`, `POSTA_LOG_LEVEL`.
- Special folders: prefer IMAP SPECIAL-USE (`\Sent`, `\Drafts`, `\Trash`, `\Archive`), then
  preset names, then config overrides.
- Keychain: service `posta-mcp`, key = username. `posta-mcp setup` prompts with `getpass`
  (never echoes, never stores elsewhere), then tests the login.

### Preset format (`presets/bogazici.toml`)

Hosts, ports, security, folder names, institutional domains, student domains, mailing-list
addresses/domains, standard/automated sender addresses. Every field not confirmed by a live test
carries `verified = false` (per section) and a comment `# unverified`. `doctor` and
`server_info` surface unverified fields. Research public Boğaziçi IT documentation for the best
current values but never treat them as confirmed.

## 5. Tools

All tools get MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`). Defaults: `limit=20`, max 100; body text truncated to `max_chars` (default
20000) with a truncation notice. Errors are short, actionable, and contain no secrets.

**Read (M1)** — `server_info` (preset, account, send enabled?, unverified fields),
`list_folders`, `search_messages(folder="INBOX", from_, to, subject, text, since, before, unseen,
flagged, limit)`, `get_message(folder, uid, max_chars, mark_seen=false)`. Optional:
`get_attachment_text(folder, uid, index)` for text-like attachments only.

**Organize + drafts (M2)** — `mark_messages(folder, uids, seen?, flagged?)`,
`move_messages(folder, uids, destination)`, `trash_messages(folder, uids)`,
`archive_messages(folder, uids)`, `create_draft(to, subject, body, cc, bcc)`,
`create_reply_draft(folder, uid, body, reply_all=false)`. Drafts are APPENDed to the Drafts
folder with `\Draft` and are never sent by these tools.

**Send (M3)** — `send_message(to, subject, body, cc, bcc, in_reply_to_uid?, folder?)` and
`send_draft(uid)`. Order of checks: sending enabled? → client supports elicitation? → build the
exact message → elicit confirmation showing From, To, Cc, Bcc, Subject, attachment names and the
full body (or first N chars plus length) → only on `accept` with `confirm=true`, send exactly the
bytes that were shown (hash binding). Decline/cancel/unsupported → nothing sent, clear message.
Save to Sent per `save_sent`.

## 6. Boğaziçi summaries (M4)

The server never calls an LLM. It selects and packages messages; the client model summarizes.
Each feature is both a **tool** (returns a delimited bundle of candidate messages with short
bodies) and an MCP **prompt** (so users can run `/hoca-ozet` etc.) that tells the model which
tool to call and how to summarize (default output language Turkish; argument `days`, default 7).

Sender categories (`classify.py`, rules from the preset, user-overridable):
- `list` — has `List-Id`, or sender/recipient matches a list address/domain
  (e.g. `students@list.bogazici.edu.tr`, `*@list.bogazici.edu.tr`).
- `standard` — automated/institutional addresses (noreply, no-reply, mailer-daemon, IT/registrar
  style addresses listed in the preset).
- `student` — sender domain in student domains (e.g. `std.bogazici.edu.tr`).
- `faculty` — sender in an institutional domain and none of the above ("likely a professor").
- `external` — anything else.

Features:
- **hoca-ozet** — summarize recent mail whose category is `faculty`.
- **ogrenci-ozet** — summarize recent mail whose category is `student`.
- **bana-ozet** — personalized: read the user's profile (interests, places, activities, e.g.
  "Güney Kampüs", "yemekhane", "tenis"), prefilter recent institutional/list mail by keyword
  match (Turkish-aware case folding: İ/i, I/ı; diacritic-insensitive; each interest has keyword
  variants incl. English), return matches plus a compact subject-only list of other recent
  school mail, and ask the model to report developments relevant to the user.
- Profile: `profile.toml` in the config dir (non-secret). Tools `get_profile`,
  `update_profile(add=[...], remove=[...])`. Ship `profile.example.toml`. Profile writes are
  logged (names only).

## 7. Testing

- Local GreenMail (Docker image in CI; standalone jar locally when Docker is unavailable) with
  IMAPS/SMTPS using a test keystore whose certificate has SANs for `localhost` and `127.0.0.1`.
  Tests pass the exported certificate via `POSTA_CA_FILE`. Generated keys/certs are gitignored.
- Integration tests are marked `greenmail`; they skip if GreenMail is unreachable unless
  `POSTA_REQUIRE_GREENMAIL=1` (CI sets it).
- Fixtures include ISO-8859-9, Windows-1254, RFC 2047 (B and Q) headers, quoted-printable and
  base64 bodies, multipart/alternative, attachments with Turkish filenames (RFC 2231).
- Never use a real email account. No real credentials in fixtures (use obvious fakes).
- Non-negotiable tests: stdout cleanliness (spawn server, handshake, every stdout line is
  JSON-RPC); no secrets in logs/outputs; no insecure TLS path (a wrong CA fails); send refusal by
  default and without elicitation, decline sends nothing; no plain EXPUNGE (spy on the client);
  reading leaves `\Seen` unset; untrusted delimiting cannot be escaped by content.

## 8. Milestones

- **M0 — Scaffold:** pyproject, package skeleton, logging, config + presets, credentials, TLS
  context, CLI, server boots over stdio with `server_info`, GreenMail tooling, Turkish fixtures,
  CI workflow, DECISIONS.md.
- **M1 — Read:** IMAP layer, MIME parsing, untrusted wrapping, read tools.
- **M2 — Organize + drafts.**
- **M3 — Send** with gate + elicitation.
- **M4 — Boğaziçi summaries:** classify, profile, digest tools, prompts.
- **M5 — Packaging + docs:** `uvx posta-mcp`, `.mcpb` bundle manifest, `server.json` for the MCP
  Registry (owner/name placeholders), README with disclaimers, client setup docs (Claude Code,
  Claude Desktop, MCP Inspector), smoke-tested in Inspector/Claude Code against GreenMail.
- **M6 — Live verification (human):** `scripts/live_verify.py` reads-only by default; checks
  login, folders/SPECIAL-USE, a PEEK fetch, Turkish decoding, optional draft append and optional
  self-send behind explicit flags; prints which preset fields can be marked verified.

## 9. README disclaimers

- Unofficial; not affiliated with or endorsed by Boğaziçi University or any institution.
- Check your institution's IT/acceptable-use policy before connecting third-party tools.
- Email content you read through these tools is sent to the model provider your client uses.
- Models can misread or mis-summarize; verify important information (deadlines, grades).
- Sending is off by default and always requires your confirmation; you are responsible for what
  you send. Provided as-is without warranty (see LICENSE).
- Preset values marked `unverified` have not been tested against the live server.

## 10. Amendments after research (see docs/RESEARCH.md)

- **Send confirmation** uses the mcp 2.3 resolver pattern (`Annotated[ElicitationResult[Confirm],
  Resolve(fn)]`), not plain `ctx.elicit()`, which fails on the modern protocol era. The resolver
  checks `ctx.client_capabilities.elicitation.form` and raises `ToolError` (suggest a draft) when
  missing. Tests cover both protocol eras (`Client(..., mode="legacy"|"auto")`).
- **Boğaziçi preset**: login username is the local part (`username_style = "localpart"`); student
  (`std.bogazici.edu.tr`) and staff (`bogazici.edu.tr`) accounts use different hosts, chosen by
  the email domain (`[[accounts]]`-style variants in the preset). All values remain unverified.
- **Env from MCPB hosts**: empty `POSTA_*` values count as unset; booleans parse leniently.
