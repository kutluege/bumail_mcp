# posta-mcp research notes (2026-10-13)

Scope: facts the implementers need. "Verified" = I ran it against the installed `mcp 2.3.0`, validated against the
published JSON Schema, or read the official primary source. Everything about Boğaziçi is UNVERIFIED until a live test
(M6), with confidence stated per item.

## 1. MCP Python SDK 2.3.0 (installed, verified by running code)

### 1.1 Breaking changes vs 1.x that matter
- `mcp.server.fastmcp` is gone (importing it raises `ModuleNotFoundError` pointing at the migration guide).
  Use `from mcp.server.mcpserver import MCPServer, Context`. `FastMCP` -> `MCPServer`; `ctx.fastmcp` -> `ctx.mcp_server`.
- Types live in the `mcp_types` package; `mcp.types` is a permanent alias. Fields are snake_case
  (`read_only_hint`, `is_error`, `structured_content`, `input_schema`). `McpError` -> `MCPError(code, message, data)`.
- `MCPServer(name, title, description, instructions, website_url, icons, version, ...)`: pass everything after `name` by keyword.
  Default name is `mcp-server`. `host/port/...` moved from the constructor to `run()` (irrelevant for stdio).
- Errors: raise `mcp.server.mcpserver.exceptions.ToolError` for an `isError=true` tool result. A raised `MCPError`
  becomes a JSON-RPC error instead.
- Sync (`def`) tools run on a worker thread automatically. Blocking IMAP in a plain `def` tool is fine.
- Roots/sampling/logging-to-client (`ctx.info()` etc.) are deprecated (emit `MCPDeprecationWarning`). Do not use `ctx.info`.
- `mcp.types.LATEST_PROTOCOL_VERSION` is now the newest revision (2026-07-28). Two protocol "eras" exist
  (see 1.5): handshake (<= 2025-11-25) and modern (>= 2026-07-28).
- Migration guide: https://py.sdk.modelcontextprotocol.io/v2/migration/

### 1.2 Server, tools, annotations, structured output, prompts, run
```python
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import ToolAnnotations
srv = MCPServer("posta-mcp", instructions="...", version="0.1.0")

@srv.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                      idempotent_hint=True, open_world_hint=True))
def get_message(folder: str, uid: int) -> MessageModel: ...   # pydantic return => outputSchema + structuredContent

@srv.prompt(name="hoca-ozet", description="...")               # args come from the function signature
def hoca_ozet(days: int = 7) -> str: ...                       # -> one user message; prompt args arrive as strings on the wire, SDK coerces

srv.run()          # stdio is the default transport; also srv.run("stdio") / await srv.run_stdio_async()
```
Verified: annotations round-trip (`tool.annotations.read_only_hint` etc.); a pydantic return type yields
`output_schema` and `result.structured_content == {...}`; a `str` return is wrapped as `{"result": "..."}`;
`structured_output=False` on `@tool` disables it; prompt arguments list as `PromptArgument(name='days', required=False)`.
`@srv.prompt` and `@srv.tool` must be called with parentheses.

### 1.3 Context and elicitation (the important part)
`Context` is injected into any tool/resolver parameter annotated `Context` (any parameter name). Useful members:
`ctx.client_capabilities` (a `ClientCapabilities | None`), `ctx.protocol_version`, `ctx.session`, `ctx.request_id`,
`ctx.elicit(message, PydanticModel)`, `ctx.report_progress()`.

**Capability check (verified):** a client that did not register an elicitation callback declares no capability.
```python
def can_elicit(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return bool(caps and caps.elicitation and caps.elicitation.form is not None)
```
(Equivalent: `ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability())))`.)

**Elicitation result shape (verified):** `ElicitationResult[T]` = `AcceptedElicitation[T]` (`.action=="accept"`, `.data` is the
validated model) | `DeclinedElicitation` (`"decline"`) | `CancelledElicitation` (`"cancel"`). Do not `isinstance` against
the `ElicitationResult` alias itself (raises `TypeError`); compare `.action` or use the member classes. The schema must be a
flat pydantic model of primitives (str/int/float/bool/enum); nested models raise `TypeError`. Optional fields (`X | None = None`)
are rendered as not-required.

**CRITICAL: plain `ctx.elicit()` only works on handshake-era connections.** Verified in-process and over real stdio:
- `legacy` client (protocol 2025-11-25) with callback: `ctx.elicit` works. Without callback the call raises
  `MCPError: Elicitation not supported`.
- `auto`/modern client (protocol 2026-07-28): `ctx.elicit` raises `NoBackChannelError` ("this transport context has no
  back-channel for server-initiated requests"), even with a callback, even over stdio.
- Claude Code's "v2" MCP runtime asks stdio servers for 2026-07-28 by default (see 2.1). So `ctx.elicit` would break
  sending in current Claude Code.

**Portable solution (verified on both eras, in-process and over stdio): resolver injection.** A tool parameter
`Annotated[ElicitationResult[Model], Resolve(fn)]` is filled before the tool body by running `fn`; `fn` returns
`Elicit(message, Model)` and the SDK performs the ask by whichever mechanism the negotiated era supports. `fn` may take
`Context` and tool arguments by name, and may `raise ToolError(...)` to abort (tool result `is_error=True`, body never runs).
Resolver bodies may re-run on every round in the modern era: keep them pure (no side effects, no sending).
If the client lacks the capability and `fn` did not guard, the SDK raises `MCPError` (-32021), so guard in `fn` to get a friendly message.
Annotate the parameter as `ElicitationResult[Model]` to receive accept/decline/cancel; annotate as plain `Model`
(`Annotated[Model, Resolve(fn)]`) and decline/cancel aborts the call for you.

### 1.4 Working snippet (ran: 8 passed)
```python
from typing import Annotated
import pytest
from mcp import Client
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitResult, ToolAnnotations
from pydantic import BaseModel, Field

SENT: list[str] = []

class Confirm(BaseModel):
    confirm: bool = Field(description="Send this exact message?")

def _can_elicit(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return bool(caps and caps.elicitation and caps.elicitation.form is not None)

def ask_confirmation(to: str, body: str, ctx: Context) -> Elicit[Confirm]:   # tool args by name
    if not _can_elicit(ctx):
        raise ToolError("Client cannot confirm sends (no elicitation). Create a draft instead.")
    return Elicit(f"To: {to}\n\n{body}\n\nSend?", Confirm)

def build_server() -> MCPServer:
    srv = MCPServer("posta-demo")
    @srv.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=False, open_world_hint=True))
    async def send_message(to: str, body: str,
                           decision: Annotated[ElicitationResult[Confirm], Resolve(ask_confirmation)]) -> str:
        if decision.action != "accept" or not decision.data.confirm:
            return f"NOT SENT ({decision.action})"
        SENT.append(to); return "SENT"
    return srv

def _client(srv, answer, mode):               # answer: None => client WITHOUT elicitation capability
    async def cb(ctx, params):
        if answer == "accept":
            return ElicitResult(action="accept", content={"confirm": True})
        return ElicitResult(action=answer)    # "decline" | "cancel"
    return Client(srv, mode=mode, **({"elicitation_callback": cb} if answer else {}))

@pytest.fixture(params=["asyncio"])
def anyio_backend(request): return request.param   # anyio's pytest plugin is installed; trio is not

@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])          # handshake era and modern era
@pytest.mark.parametrize("answer,sent", [("accept", True), ("decline", False), ("cancel", False)])
async def test_elicit_paths(mode, answer, sent):
    SENT.clear()
    async with _client(build_server(), answer, mode) as c:
        r = await c.call_tool("send_message", {"to": "a@example.org", "body": "hi"})
    assert (SENT == ["a@example.org"]) is sent and not r.is_error

@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_no_elicitation_client_refuses(mode):
    SENT.clear()
    async with _client(build_server(), None, mode) as c:
        r = await c.call_tool("send_message", {"to": "a@example.org", "body": "hi"})
    assert r.is_error and SENT == [] and "draft" in r.content[0].text
```
In-memory client notes: `Client(server_or_MCPServer, mode="legacy"|"auto", elicitation_callback=cb)`; no callback => no
capability declared. `mode="auto"` (default) on an in-process server uses the modern direct dispatcher (no JSON-RPC framing).
Use both modes in tests. Result accessors: `r.content[0].text`, `r.structured_content`, `r.is_error`; `await c.list_tools()`,
`c.list_prompts()`, `c.get_prompt(name, {"days": "3"})` (arguments are strings). `Client` also accepts
`StdioServerParameters(command=sys.executable, args=[...])` for real subprocess tests (verified).

Spec notes for M3: the elicitation form can only collect primitives, so show From/To/Cc/Bcc/Subject/body in the `message`
string and ask for a single `confirm: bool`. Hash binding: build the message bytes in the resolver *and* again in the tool body
(deterministic), compare digests, and send the body-built bytes only if equal. Treat `accept` with `confirm=False` as not sent.

### 1.5 Logging and stdout
`MCPServer.__init__` calls `logging.basicConfig(level=log_level, handlers=[RichHandler(Console(stderr=True)) or StreamHandler()])`.
Both go to **stderr** (rich is not installed here, so a plain `StreamHandler` -> stderr). Verified: raw stdout of a stdio server
contains only JSON-RPC lines; a `log.warning(...)` in a tool appeared only on stderr. `basicConfig` is a no-op if the root logger
already has handlers, so configure our own stderr handler (logs.py) BEFORE constructing `MCPServer`, and set `log_level` explicitly.
Never `print()` to stdout.
Protocol era note: modern-era clients only receive server log notifications if they opt in per request; irrelevant since we log to stderr.

Sources: installed `mcp/server/mcpserver/{server,context,resolve}.py`, `mcp/server/elicitation.py`, `mcp/client/client.py`;
migration guide above.

## 2. Client support

### 2.1 Claude Code (official docs, https://code.claude.com/docs/en/mcp)
- **Elicitation: yes** ("Respond to MCP elicitation requests"): form mode (dialog with fields) and URL mode; no config needed;
  the `Elicitation` hook can auto-respond (note: such a hook could auto-approve sends; our server cannot detect it. Document it).
  Release notes put it at v2.1.76+. On the modern runtime Claude Code declares `elicitation: {form: {}, url: {}}`.
- **Runtimes:** "v1" (TS SDK 1.x) and "v2" (TS SDK 2.0, protocol 2026-07-28). v2 is used on v2.1.232+ when feature flags are fetched,
  and by default on 2.1.274+ for Bedrock/Vertex/Foundry/gateway/telemetry-off sessions. v2 asks stdio servers for the newer revision
  by default. Override: `MCP_SDK_GENERATION=v1|v2`, `MCP_PROTOCOL_NEGOTIATION=auto|legacy`. Hence use the Resolve/Elicit pattern (1.3).
- **Prompts as slash commands: yes.** Listed as `/servername:promptname (MCP)`; typing `/mcp__servername__promptname` also runs it.
  Arguments are passed space-separated after the command (split on whitespace). In the `mcp__` form, characters in the server name outside
  `[A-Za-z0-9_-]` become `_`; the prompt name is used as declared. So server `posta` + prompt `hoca-ozet` => `/mcp__posta__hoca-ozet 7`
  (also `/posta:hoca-ozet`). Prompt `days` is a single token. The server name `anthropic-skills` is reserved (prompts hidden).
- **Add a stdio server with env vars:**
  `claude mcp add --transport stdio --env POSTA_EMAIL=ad.soyad@std.bogazici.edu.tr posta -- uvx posta-mcp serve`
  (`--env` must not directly precede the server name; keep `--transport stdio` between them; everything after `--` is the server command).
  Do not pass the password on the command line; use the keychain via `posta-mcp setup`.
- Tool descriptions and server instructions are truncated at 2,048 chars; tool input schemas must be valid draft 2020-12 with
  property names `[A-Za-z0-9_.-]{1,64}` or the tool is excluded. A tool may set `_meta["anthropic/requiresUserInteraction"]=true`
  to force a permission prompt on every call (even in bypass/auto modes); consider this for `send_message`/`send_draft`
  as defense in depth (set via `@srv.tool(meta={...})`; I have not run this against Claude Code).

### 2.2 Claude Desktop
- Tools: yes. MCPB bundles: yes. Prompts: supported by Desktop (appear in the attachment/"+" menu as I recall; low confidence).
- **Elicitation: unconfirmed, assume NO.** Third-party reports (Oct 2025: "-32601 Method not found"; GitHub issues #41110/#41347
  Mar 2026 asking for it) say Desktop did not support it; Glama lists it as supported (conflict). Therefore the no-capability
  refusal path is the normal Desktop path: sending stays disabled there, drafts only.
  Sources: https://claudeissues.com/issue/41110-feature-mcp-elicitation-support-in-claude-desktop-app ,
  https://glama.ai/mcp/clients/claude-desktop

### 2.3 MCP Inspector (https://modelcontextprotocol.io/docs/tools/inspector, needs Node >= 22.19)
```
npx @modelcontextprotocol/inspector -e POSTA_EMAIL=ad.soyad@bogazici.edu.tr -e POSTA_LOG_LEVEL=DEBUG uv run posta-mcp serve
npx @modelcontextprotocol/inspector --cli -e POSTA_EMAIL=x uv run posta-mcp serve --method tools/list
npx @modelcontextprotocol/inspector --config inspector.json        # read-only {"mcpServers": {"posta": {"command": "uv", "args": [...], "env": {...}}}}
```
- Launcher flags (`--web/--cli/--tui`) must come first. `-e KEY=VALUE` (repeatable) sets stdio server env; `--cwd` sets its directory;
  `--` passes flags that the Inspector would otherwise consume. Web UI at http://127.0.0.1:6274 with a per-launch token in the printed URL
  (`MCP_AUTO_OPEN_ENABLED=false` stops auto-open; `MCP_INSPECTOR_API_TOKEN` pins the token).
- Protocol era: `--protocol-era legacy|auto|modern`; default is `legacy` (plain initialize). Test the server in both.
  The docs state the legacy "server asks client" elicitation pattern errors on a modern connection, confirming 1.3.
- Inspector stores stdio `env:` values (including a password if you type one) in the OS keychain, or in plaintext
  `~/.mcp-inspector/secrets.json` if no keychain. Use only fake credentials against GreenMail there.
- Elicitation in the Inspector: it has a pending-request modal for MRTR/elicitation. I did not verify form-elicitation UI details.

## 3. MCPB bundle (.mcpb, formerly .dxt)

Sources: https://github.com/modelcontextprotocol/mcpb (MANIFEST.md, CLI.md, schemas/); npm `@anthropic-ai/mcpb` latest 2.1.2 (2025-12-04).

- **Schema versions:** repo has `schemas/mcpb-manifest-v0.1 ... v0.4.schema.json` (draft-07). MANIFEST.md header says "Current version 0.3" but its
  UV section and `examples/hello-world-uv` use `manifest_version: "0.4"`; the v0.3 schema enum is `python|node|binary`, **v0.4 adds `uv`**.
  Use `"0.4"` for the uv type.
- **`uv` server type (v0.4+):** "Python server using UV runtime"; host installs deps from `pyproject.toml` (small bundle, handles compiled deps such as
  pydantic, no user Python needed). Rules: must include `pyproject.toml`, must NOT include `server/lib/` or `server/venv/`, `mcp_config` optional
  (the official example still sets it to `uv run --directory ${__dirname} ...`). `type: python` requires fully bundled deps and cannot ship compiled
  deps portably, so it is unsuitable for us. UNVERIFIED: whether Claude Desktop honors a custom `mcp_config` for `uv` and ships its own `uv`. Verify in Desktop (M5).
- **Secrets:** `user_config.<key>` with `type:"string", sensitive:true` is masked in the host UI and stored securely by the host;
  reference it in `mcp_config.env` as `"${user_config.password}"`. Schema requires `type`, `title`, `description` on every field (validated: omitting
  `description` fails). Other types: number, boolean, directory, file. Other templates: `${__dirname}`, `${HOME}`, `${DESKTOP}`, `${DOCUMENTS}`, `${DOWNLOADS}`, `${/}`.
  Implementation implications: an unset optional field probably expands to an empty string, so treat empty `POSTA_*` env values as unset
  (otherwise an empty `POSTA_PASSWORD` would mask the keychain); a boolean's string form is unverified, so parse `true/false/1/0/yes/no` case-insensitively.
- **`compatibility`** (all optional): `claude_desktop` (semver range), `platforms` (`darwin|win32|linux`), `runtimes.python` (range).
- **`tools`** (`name`, `description`) and **`prompts`** (`name`, `text` required, optional `description`, `arguments: [names]`) are informational listings
  (`text` can use `${arguments.NAME}`); the live lists still come from the server (`tools_generated`/`prompts_generated` flags exist).
- **CLI:** `npm install -g @anthropic-ai/mcpb`; `mcpb init`, `mcpb validate manifest.json`, `mcpb pack <dir> [output.mcpb]` (ZIP; honors `.mcpbignore`; exclude `.venv`,
  `tests/`, `docs/`), also `mcpb sign|verify|info|unsign`.

Minimal manifest, validated with `jsonschema` against `mcpb-manifest-v0.4.schema.json` (OWNER is a placeholder):
```json
{
  "$schema": "https://raw.githubusercontent.com/modelcontextprotocol/mcpb/main/schemas/mcpb-manifest-v0.4.schema.json",
  "manifest_version": "0.4",
  "name": "posta-mcp",
  "display_name": "posta-mcp (IMAP/SMTP mail)",
  "version": "0.1.0",
  "description": "Local-first MCP server for your email over IMAP/SMTP, with a Bogazici University (BUmail) preset.",
  "long_description": "Unofficial. Reads and drafts mail over IMAP; sending is off by default and always needs your confirmation.",
  "author": { "name": "OWNER" },
  "license": "MIT",
  "repository": { "type": "git", "url": "https://github.com/OWNER/posta-mcp" },
  "server": {
    "type": "uv",
    "entry_point": "src/posta_mcp/__main__.py",
    "mcp_config": {
      "command": "uv",
      "args": ["run", "--directory", "${__dirname}", "posta-mcp", "serve"],
      "env": {
        "POSTA_PRESET": "${user_config.preset}",
        "POSTA_EMAIL": "${user_config.email}",
        "POSTA_PASSWORD": "${user_config.password}",
        "POSTA_ALLOW_SEND": "${user_config.allow_send}"
      }
    }
  },
  "user_config": {
    "preset": { "type": "string", "title": "Institution preset", "description": "bogazici or generic", "default": "bogazici", "required": true },
    "email": { "type": "string", "title": "Email address", "description": "Your full institutional email address", "required": true },
    "password": { "type": "string", "title": "Mail password / app password", "description": "Stored by the host in the OS secret store and passed to the server as POSTA_PASSWORD", "sensitive": true, "required": true },
    "allow_send": { "type": "boolean", "title": "Allow sending", "description": "Sending is still confirmed per message. Off by default.", "default": false }
  },
  "tools": [
    { "name": "server_info", "description": "Show preset, account and which settings are unverified" },
    { "name": "list_folders", "description": "List mailbox folders" },
    { "name": "search_messages", "description": "Search messages (read-only)" },
    { "name": "get_message", "description": "Read one message (does not mark it seen)" },
    { "name": "send_message", "description": "Send a message (disabled by default; needs confirmation)" }
  ],
  "prompts": [
    { "name": "hoca-ozet", "description": "Summarize recent mail from faculty", "arguments": ["days"], "text": "Use the hoca_ozet tool with days=${arguments.days} and summarize the result in Turkish." }
  ],
  "keywords": ["email", "imap", "smtp", "bogazici"],
  "compatibility": { "platforms": ["darwin", "win32", "linux"], "runtimes": { "python": ">=3.11" } }
}
```
Caveat: the host must be able to build the project (`uv run --directory` installs the project via hatchling); the `${__dirname}` bundle must contain
`pyproject.toml`, `README.md` (pyproject `readme`), `LICENSE` and `src/`. Username note: the Boğaziçi login name is the local part, see 5.

## 4. MCP Registry `server.json`

Sources: https://github.com/modelcontextprotocol/registry (docs/reference/server-json/*, docs/modelcontextprotocol-io/{quickstart,package-types}.mdx); registry v1.8.1 (2026-08-06).

- **Schema:** `"$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"` (latest dated release; verified the URL serves
  JSON Schema draft-07; a `draft/` schema exists but is unreleased).
- **Name:** `^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$`, exactly one slash, max 200. With GitHub auth the namespace must be `io.github.<github-user-or-org>/<server>`
  (and the namespace is verified by `mcp-publisher login github`). Description max **100 chars**. `version` is a single concrete version (no ranges).
- **PyPI package entry:** `registryType: "pypi"`, `identifier` (PyPI name), `version`, `transport: {"type":"stdio"}`; optional `registryBaseUrl`
  (`https://pypi.org` only), `runtimeHint: "uvx"`, `packageArguments`, `environmentVariables[]` (`name`, `description`, `isRequired`, `isSecret`, `default`).
- **PyPI ownership verification:** the package README (= PyPI long description) must contain `mcp-name: <exact server.json name>`, may be an HTML comment on its own line:
  `<!-- mcp-name: io.github.OWNER/posta-mcp -->`. The token must be followed by newline/whitespace/tag/`-->` (not a trailing period). The README must be in the
  *published* version's metadata, so publish to PyPI first, then `mcp-publisher publish`.
- **Publishing:** `mcp-publisher init | login github | validate | publish` (binary from the registry GitHub releases or Homebrew). Registry is in preview.

Minimal `server.json`, validated against the 2025-12-11 schema:
```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.OWNER/posta-mcp",
  "title": "posta-mcp",
  "description": "Local IMAP/SMTP email MCP server with a Bogazici University preset; sending is opt-in.",
  "version": "0.1.0",
  "repository": { "url": "https://github.com/OWNER/posta-mcp", "source": "github" },
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "posta-mcp",
      "version": "0.1.0",
      "runtimeHint": "uvx",
      "transport": { "type": "stdio" },
      "packageArguments": [ { "type": "positional", "value": "serve" } ],
      "environmentVariables": [
        { "name": "POSTA_PRESET", "description": "Institution preset (bogazici or generic)", "default": "bogazici" },
        { "name": "POSTA_EMAIL", "description": "Your full email address", "isRequired": true },
        { "name": "POSTA_PASSWORD", "description": "Mailbox or app password (or store it in the OS keychain via posta-mcp setup)", "isSecret": true },
        { "name": "POSTA_ALLOW_SEND", "description": "Set to true to allow sending (each send still needs confirmation)", "default": "false" }
      ]
    }
  ]
}
```
README must carry `<!-- mcp-name: io.github.OWNER/posta-mcp -->`. Check `uvx posta-mcp serve` works (entry point `posta-mcp = posta_mcp.cli:main`; bare `posta-mcp` should also default to `serve`).

## 5. Boğaziçi University mail (UNVERIFIED until live test)

Confidence: H = from official Bilgi İşlem document text/screenshots or DNS; M = inferred from a pattern in official material; L = guess.

| Item | Value | Conf. | Source |
|---|---|---|---|
| IMAP host, staff/faculty (`@bogazici.edu.tr`, also `pt`) | `imap.bogazici.edu.tr` (A 193.140.192.180) | H | Outlook IMAP guide text; iOS guide screenshot |
| IMAP host, students (`@std.bogazici.edu.tr`) | `imap.std.bogazici.edu.tr` (A 193.140.192.181) | H | Outlook IMAP guide text; DNS |
| IMAP port/security | 993, SSL/TLS (implicit) | H (staff; official Outlook "Advanced" screenshot + Android Gmail screenshot) | below |
| SMTP host, staff/faculty | `smtp.bogazici.edu.tr` (A 193.140.192.182); part-time: `smtp-pt.bogazici.edu.tr` | H | same guides |
| SMTP host, students | `smtp.std.bogazici.edu.tr` (A 193.140.192.183) | H | same |
| SMTP port/security | 587, STARTTLS, "server requires authentication", same credentials as incoming | H | Outlook Advanced screenshot; iOS screenshot shows 587 + "SSL Kullan" toggle on |
| Student IMAP/SMTP ports/security | assume identical to staff (993 SSL, 587 STARTTLS) | M | guides list student hosts but screenshots only show staff |
| Username | **local part only**, `ad.soyad` (NOT the full address); BUMail login says "ad.soyad (uzantı olmadan)" | H | Outlook/iOS/Android screenshots, BUMail guide |
| Retired / pt hosts | `imap-retired.bogazici.edu.tr` 993, `smtp-retired.bogazici.edu.tr` 587 (also 25), `pop-retired...` 995; `smtp-pt.bogazici.edu.tr` | H | https://mail.bogazici.edu.tr/info |
| POP | `pop.bogazici.edu.tr`, `pop.std.bogazici.edu.tr`, 995 SSL | H | same guides (we do not use POP) |
| Webmail | `bumail.bogazici.edu.tr` (also Horde/Roundcube mentioned on /info) | H | https://bilgiislem.bogazici.edu.tr/tr/pages/e-posta/2367 |
| Hosting platform | **On-premise / university-run**, not Google Workspace and not Microsoft 365 | M-H | below |
| Basic password auth over IMAP | Official guides use plain username+password; no app-password/OAuth/MFA mention anywhere | M | all guides; announcements |
| Student quota | 1 GB; staff 2 GB; faculty 10 GB | H | https://mail.bogazici.edu.tr/info |

Evidence for "not Google/Microsoft-hosted" (public DNS via 8.8.8.8, 2026-10-13): MX for `bogazici.edu.tr` and `std.bogazici.edu.tr` is `mailgw.bogazici.edu.tr` /
`mailgw.std.bogazici.edu.tr` (university gateway); legacy `boun.edu.tr` MX also -> `mailgw.bogazici.edu.tr`; imap/smtp/pop hosts are A records in 193.140.192.0/20
(university space); SPF is `a mx ip4:... include:spf.ppostaa.com -all` (university ranges plus a hosted filter). The domain does carry `MS=` and `google-site-verification`
TXT records, but those are domain-ownership proofs, not hosting. `autodiscover.bogazici.edu.tr` is a CNAME to `webmail.bogazici.edu.tr`. NOTE (new, medium risk): an IT notice
(22 May 2026) describes a "system upgrade of the university's **Exchange** e-mail servers" (23-31 May 2026) affecting Outlook, webmail and mobile access, saying no password
or device-setting changes are needed. Exchange (on-prem) does expose IMAP4/SMTP submission, and the published settings are still the guide's, but the guides pre-date this upgrade
(iOS guide rev 2024-01-24, other PDFs undated, Mailman guide 2025-08-07), so re-verify IMAP login live. Exchange on-prem commonly supports `\Sent`/`\Drafts`/`\Trash` SPECIAL-USE poorly
and may lack `MOVE`; the spec's fallback chain (COPY + `\Deleted` + `UID EXPUNGE`, else refuse) matters here. UIDPLUS/MOVE support is unknown.

Account domains (https://bilgiislem.bogazici.edu.tr/tr/pages/e-posta/2367; announcement https://bilgiislem.bogazici.edu.tr/en/announcements/transition-of-email-addresses-to-bogaziciedut/2500, 28 Oct 2023) (H):
- `@bogazici.edu.tr` academic + administrative staff (faculty); `@pt.bogazici.edu.tr` part-time/visiting; `@retired.bogazici.edu.tr`; `@std.bogazici.edu.tr` students.
- Alumni: the announcement says `@alum.bogazici.edu.tr`, the e-posta page says `alumni.bogazici.edu.tr` (conflict; treat both as alumni; zero quota, forwarding only).
- Legacy `@boun.edu.tr` (and `@alumni.boun.edu.tr`): sending closed 1 Feb 2024, but the domain still receives (MX -> `mailgw.bogazici.edu.tr`) and old mail/contacts/lists still carry
  it. Treat `boun.edu.tr` as an institutional domain for classification (staff vs student cannot be told from the legacy domain alone).
- Department websites use `<dept>.bogazici.edu.tr` hosts but people's mail addresses are plain `@bogazici.edu.tr`. A suffix rule `*.bogazici.edu.tr` is safer than an exact match (M).

Mailing lists:
- System is **Mailman 3** at `https://mailman.bogazici.edu.tr` (reachable via BU-VPN per the guide); list mail domain `list.bogazici.edu.tr` has MX `mailman.bogazici.edu.tr` (H).
  Mailman 3 sets `List-Id: <listname.list.bogazici.edu.tr>`-style headers (M; standard Mailman behavior, verify on a real message).
- Official lists named on /e-posta: **personnel** (all staff, automatic), **students** (all students, automatic), **instructor** (refreshed each term), **instforum** (academic staff, by request).
  Full addresses are therefore probably `students@list.bogazici.edu.tr`, `personnel@list.bogazici.edu.tr`, `instructor@list.bogazici.edu.tr`, `instforum@list.bogazici.edu.tr` (M-L: names verified,
  domain pairing inferred; instforum has also been described in press as an informal academic list; I found no public page printing these exact addresses). Department/course lists also live under the same domain.
- Per mailing-list guide, send-to-list rights are controlled per list (Message Acceptance), so students often cannot post to `students@`.

Typical automated senders: nothing public. Verified only: IT contact `bilgiislem@bogazici.edu.tr`; IT announcements are published from "Bilgi İşlem Daire Başkanlığı". Registration/OBS/library
senders are not documented publicly (L). Ship preset patterns as heuristics only (`noreply@`, `no-reply@`, `donotreply@`, `mailer-daemon@`, `postmaster@`, `bilgiislem@bogazici.edu.tr`, `*@list.bogazici.edu.tr` => list)
and mark them unverified; classification should prefer headers (`List-Id`, `Auto-Submitted`, `Precedence: bulk`) over address rules.

Other facts: password change/recovery at `https://mail.bogazici.edu.tr/password` and `/recovery`; password-expiry notices (14 May 2026) mean a locked/expired password will look like an IMAP auth failure
(the error message should say "password may have expired, change it at mail.bogazici.edu.tr"). A 2026-06-03 IT notice warns of phishing from compromised institutional accounts: reinforces the untrusted-content rule.
IT phone/email: 0 212 359 47 00, bilgiislem@bogazici.edu.tr. The `/info` page notes unspecified limits (message size, send rate, concurrent connections): keep one connection per operation, back off on errors.
Policy reminder: no public statement allows or forbids third-party clients beyond publishing IMAP guides for Outlook/Gmail app/iOS; keep the README disclaimer.

### Findings that should change the spec
1. **Username default.** `username` defaults to the email address in SPEC section 4, but Boğaziçi expects the local part (`ad.soyad`). Add a preset field `username_style = "localpart"` (and let `setup` derive the username from the email), keep an override.
2. **Dual-era elicitation.** SPEC section 5 says "elicit"; implement it with the Resolve/Elicit resolver pattern (1.3) rather than `ctx.elicit`, and test both `mode="legacy"` and `"auto"`.
3. **Student hosts differ from staff hosts** (`imap.std...`, `smtp.std...`); the preset needs per-user-type host sets, chosen by the email domain.
4. **`boun.edu.tr` legacy domain** still receives mail; include it as an institutional domain; add `alum.bogazici.edu.tr`, `alumni.bogazici.edu.tr`, `pt.`, `retired.` domains.
5. No OAuth requirement found; password auth over IMAP 993/SMTP 587 is the documented path. Re-verify after the May 2026 Exchange upgrade.
6. MCPB: use `manifest_version "0.4"` with `server.type "uv"`; treat empty env values as unset (and parse boolean env strings leniently).

### Source URLs (Boğaziçi)
- https://bilgiislem.bogazici.edu.tr/tr/pages/e-posta/2367 (account types, lists, webmail)
- https://mail.bogazici.edu.tr/info (retired hosts/ports, quotas, Horde/Roundcube, password pages)
- https://bilgiislem.bogazici.edu.tr/tr/pages/kullanim-kilavuzlari/2428 (index of guides)
- https://mediastore.cc.bogazici.edu.tr/web/userfiles/files/outlook_yeni_kurulum_guncel_0.pdf (POP+IMAP hosts per user type; Advanced tab: IMAP 993 SSL/TLS, SMTP 587 STARTTLS)
- https://mediastore.cc.bogazici.edu.tr/web/userfiles/files/1-outlook_yeni_kurulum_guncel_eng.pdf (English; POP 995 SSL/TLS, SMTP 587 STARTTLS)
- https://bilgiislem.bogazici.edu.tr/sites/bilgiislem.boun.edu.tr/files/ios_bogazici_eposta_hesabinin_eklenmesi.pdf (iOS: imap.bogazici.edu.tr, smtp.bogazici.edu.tr:587, username ad.soyad)
- https://mediastore.cc.bogazici.edu.tr/web/userfiles/files/android_cep_telefonlarinda_gmaile_bogazici_e-posta_hesabinin_kurulumu_2.pdf (IMAP 993 SSL/TLS, username ad.soyad)
- https://mediastore.cc.bogazici.edu.tr/web/userfiles/files/bumail_kullanim_kilavuzu.pdf and .../mailman_-_kullanim_kilavuzu.pdf (login name format; Mailman)
- https://bilgiislem.bogazici.edu.tr/en/announcements/transition-of-email-addresses-to-bogaziciedut/2500 (domain split, boun.edu.tr cutoff)
- https://bilgiislem.bogazici.edu.tr/en/announcements/regarding-the-system-update-maintenance-for-i/5251 (Exchange upgrade, May 2026)
- https://bilgiislem.bogazici.edu.tr/en/announcements/corporate-account-password-update-announcemen/5141 and .../important-cybersecurity-reminder-email-securi/5359
- DNS: public resolver queries for MX/A/TXT/CNAME of the hostnames above, run 2026-10-13.
