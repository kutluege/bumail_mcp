# Client setup

posta-mcp is a stdio MCP server. Every client launches it as a subprocess (`posta-mcp serve`). The
password goes in the OS keychain (`posta-mcp setup`) or in `POSTA_PASSWORD`; never on a command line
you will keep in shell history. Server name used below: `posta`.

## Claude Code

`uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp setup` (or `uv run posta-mcp setup` from a checkout) stores the password, writes
`config.toml` (which holds the email, so no `--env POSTA_EMAIL` is needed) and offers to register the
server for you. To do it by hand:

```
claude mcp add --scope user posta -- uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp serve
```

Rules for `claude mcp add`: the flags (`--scope`, `--env`) go before the server name, and everything
after `--` is the server command. `--scope user` makes it available in every project (the default
`local` scope only covers the current folder). Add `--env POSTA_ALLOW_SEND=true` only if you want
sending. An `--env POSTA_EMAIL=...` from an older registration overrides `config.toml`; accepting
"replace" in `setup` removes it.

From a source checkout instead of PyPI (`setup` prints this form itself when run from a checkout):

```
claude mcp add --scope user posta -- uv run --directory /path/to/posta-mcp posta-mcp serve
```

- **Prompts become slash commands.** `/posta:hoca-ozet 7`, `/posta:ogrenci-ozet 7`,
  `/posta:bana-ozet 3`, and `/posta:baslangic` for a first-run check (also written `/mcp__posta__hoca-ozet 7`). Arguments are positional,
  space-separated: `days`, then optional `language`.
- **Elicitation works** in Claude Code (v2.1.76 and later). With sending enabled, `posta_gonder` and
  `taslagi_gonder` show a confirmation dialog with the full message. Do not install an `Elicitation` hook
  that auto-accepts; the server cannot tell and it would defeat the confirmation.
- Check the connection with `/mcp`, or `claude mcp list`.
- The newer Claude Code runtime speaks the 2026-07-28 MCP protocol; posta-mcp supports both eras. To
  force the older one for debugging: `MCP_PROTOCOL_NEGOTIATION=legacy`.

## Claude Desktop

Install the `.mcpb` bundle: download `posta-mcp-<version>.mcpb` from the release, then open it in
Claude Desktop (or Settings > Extensions > Advanced > Install Extension). The bundle is Boğaziçi-only
(`POSTA_PRESET=bogazici` is fixed; other providers use Claude Code). The Turkish install dialog asks for:

| Field | Meaning |
|---|---|
| Email address | your full address; the Boğaziçi student/staff hosts are chosen from its domain |
| Mail password | stored by Desktop as a secret and passed to the server as `POSTA_PASSWORD` |
| Allow sending | off by default |

Notes and caveats:

- The bundle is of type `uv`: Claude Desktop runs `uv run` on the bundled project and installs the
  dependencies itself. It has not been confirmed that Desktop ships its own `uv`; if the extension
  fails to start, install [uv](https://docs.astral.sh/uv/) and restart Desktop.
- **Elicitation support in Claude Desktop is unconfirmed** (reports conflict). If Desktop cannot show
  the confirmation, `posta_gonder` and `taslagi_gonder` **refuse** and tell you to use a draft. Reading,
  searching, organizing and drafting still work. Treat sending as unavailable on Desktop.
- Prompts (`hoca-ozet`, ...) should be offered by Desktop's attachment menu; this is not verified. If
  you do not see them, ask for the tool in plain words ("run hoca_ozet for the last 7 days").
- If the email or password is missing or wrong, the server still starts: `sunucu_bilgisi` and the
  `baslangic` prompt explain in Turkish what to fix in the extension settings.
- Manual alternative without the bundle: add a server to `claude_desktop_config.json` with
  `"command": "uvx"`, `"args": ["posta-mcp", "serve"]` and an `env` block.

## MCP Inspector

Needs Node 22.19 or newer. Useful for seeing the exact tool schemas and raw outputs. Use only fake
credentials with a local test server: the Inspector stores `env` values it is given (in the OS
keychain, or `~/.mcp-inspector/secrets.json` without one).

Tested with `@modelcontextprotocol/inspector` 2.10.1 against GreenMail (see `docs/TESTING.md`).
Config-file form (works with both web UI and `--cli`); write `inspector.json` (keep it out of git):

```json
{
  "mcpServers": {
    "posta": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/posta-mcp", "posta-mcp", "serve"],
      "env": {
        "POSTA_CONFIG": "/abs/path/to/nonexistent.toml",
        "POSTA_PRESET": "generic",
        "POSTA_EMAIL": "ogrenci@std.bogazici.test",
        "POSTA_PASSWORD": "ogrenci-fake-pass",
        "POSTA_IMAP_HOST": "127.0.0.1",
        "POSTA_IMAP_PORT": "<IMAPS port from `greenmail.py status`>",
        "POSTA_SMTP_HOST": "127.0.0.1",
        "POSTA_SMTP_PORT": "<SMTPS port>",
        "POSTA_SMTP_SECURITY": "ssl",
        "POSTA_CA_FILE": "/abs/path/to/posta-mcp/.greenmail/ca.pem"
      }
    }
  }
}
```

Web UI (opens a browser; the URL with its token is printed):

```
npx -y @modelcontextprotocol/inspector --config inspector.json --server posta
```

Non-interactive CLI mode (what the smoke test used). Mode flags (`--cli`) must come first:

```
npx -y @modelcontextprotocol/inspector --cli --config inspector.json --server posta --method tools/list
npx -y @modelcontextprotocol/inspector --cli --config inspector.json --server posta --method tools/call --tool-name postalari_ara --tool-arg folder=INBOX limit=5
npx -y @modelcontextprotocol/inspector --cli --config inspector.json --server posta --method tools/call --tool-name postayi_oku --tool-arg folder=INBOX uid=1
npx -y @modelcontextprotocol/inspector --cli --config inspector.json --server posta --method tools/call --tool-name taslak_olustur --tool-arg 'to=["hoca@bogazici.test"]' subject=Merhaba "body=Merhaba hocam"
```

List-valued arguments are passed as JSON. Add `--method prompts/list` or `prompts/get --prompt-name
hoca-ozet --prompt-args days=7` once the summary prompts are installed.

`--protocol-era legacy|auto|modern` selects which MCP protocol revision the Inspector negotiates (default
`legacy`). The server works in all three (checked with `sunucu_bilgisi` over `auto` and `modern`); the send
confirmation uses the pattern that works on both eras, so test sending in more than one.

Quirk: on Windows with Inspector 2.10.1 the inline form (`... --cli -e KEY=VALUE uv run posta-mcp serve
--method tools/list`) from older docs failed with "No servers found in config file" (it works with
`--config` and `--server`). Older Inspector versions accepted `-e` and an inline command.

### Smoke test used for M5

```
export POSTA_GREENMAIL_INSTANCE=m5
uv run python scripts/greenmail.py certs
uv run python scripts/greenmail.py start --instance m5      # prints the IMAPS/SMTPS ports
# seed a Turkish fixture (tests/fixtures/eml/*.eml) with an IMAP APPEND as ogrenci@std.bogazici.test
# run the Inspector commands above, then:
uv run python scripts/greenmail.py stop --instance m5
```

Results: `tools/list` returned all tools registered by the installed tool modules; `postalari_ara`
listed the three seeded Turkish messages with correct decoding (windows-1254, QP/UTF-8 and `Ynt:`
subjects); `postayi_oku` returned the body wrapped in an `untrusted-email` block; `taslak_olustur`
saved a draft in Drafts and reported "NOT been sent"; afterwards the seeded messages still had no
`\Seen` flag.

## Other clients

Any MCP client that can launch a stdio command works: use `uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp serve` as the command
and pass the `POSTA_*` variables as environment. A client without elicitation support can read and
draft but not send.
