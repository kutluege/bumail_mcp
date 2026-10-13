# Decisions

Deviations from `docs/SPEC.md` and notable judgment calls, one or two lines each.

- The original SPEC.md was not in the repo; the orchestrator wrote `docs/SPEC.md` from the task brief, adding the hoca-ozet / ogrenci-ozet / bana-ozet features as M4.
- Docker is not available on the dev machine: GreenMail runs locally as the standalone jar; CI uses the Docker image.
- mcp 2.3's high-level server is `mcp.server.mcpserver.MCPServer` (FastMCP was renamed); `ToolAnnotations` from `mcp.types`.
- The SDK calls `logging.basicConfig`; we install a stderr-only root handler first so that call is a no-op and nothing can reach stdout.
- Logged secrets are scrubbed by a redaction filter (`logs.register_secret`), including tracebacks.
- `config.toml` is strict (`extra="forbid"`); any key containing `password` is rejected without echoing its value.
- `POSTA_ALLOW_SEND=0` explicitly overrides `sending.enabled=true` in the file.
- Tool modules expose `register(server, deps)`; `Deps.mailbox()` returns a context-managed `posta_mcp.imap.Mailbox(config)`.
- Ruff rules pinned (`E4,E7,E9,F,I,B,UP`) and `.claude/` (agent worktrees) excluded so lint is stable across ruff versions.
