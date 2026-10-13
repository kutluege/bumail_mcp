# Handoff: what the human must do next

Everything below needs a person: a real mail account, a real GitHub/PyPI identity, or a decision.
Nothing here has been published or pushed, CI has never run, and no preset value has been checked
against a live server.

## Status at handoff

- M0–M5 are done. Locally there are 552 tests passing against GreenMail (`POSTA_REQUIRE_GREENMAIL=1`), with ruff lint and format clean. Every non-negotiable has tests, and a separate read-only security review found no violations; its smaller findings are fixed (see `docs/DECISIONS.md`).
- MCP Inspector (CLI, both protocol eras) and Claude Code (`claude -p` with `--mcp-config`) were run against GreenMail with the `bogazici` preset. Read tools, the three summaries and the send refusal all worked, and the model recognized and ignored the prompt-injection test email.
- Not yet tested, and it needs you: the **send confirmation dialog** in interactive Claude Code. Do this against GreenMail before using a real account. Follow `docs/TESTING.md` to start GreenMail, then point Claude Code at it as in `docs/CLIENTS.md`, adding `POSTA_ALLOW_SEND=1`. Ask it to send a mail to `hoca@bogazici.test` and check that the dialog shows the exact recipients and body. Then check that Decline sends nothing.

## a) Run the live verification (M6)

Goal: find out which `bogazici` preset values are actually right, then mark them `verified = true`.

1. Install deps: `uv sync`.
2. Put your password in the OS keychain yourself (it is never passed on a command line):
   ```
   uv run posta-mcp setup
   ```
   Choose preset `bogazici`, enter your full address (`ad.soyad@std.bogazici.edu.tr` for students,
   `ad.soyad@bogazici.edu.tr` for staff). The login name defaults to the part before `@`; accept it.
   `setup` ends with an IMAP login test. If you are off campus and the university requires the VPN
   for IMAP, connect to BU-VPN first.
3. Check the config: `uv run posta-mcp doctor` (lists the unverified fields).
4. Run the read-only checks:
   ```
   uv run python scripts/live_verify.py
   ```
   It does: TLS connect and IMAP login; capabilities (MOVE, UIDPLUS, SPECIAL-USE, IDLE); folder list
   against the preset's Sent/Drafts/Trash/Archive; one `BODY.PEEK` fetch (preferring an unread
   message) and a check that `\Seen` did not change; Turkish decoding check; SMTP TLS connect and
   login (nothing is sent). It prints uid, size and charset names only; add `--show-subjects` to also
   see the one subject. It never prints the password or bodies.
5. Optional, only if you want them:
   - `--append-draft` puts one clearly marked test draft in your Drafts folder (delete it by hand).
   - `--send-self` sends ONE test mail to your own address after you retype it.
6. Read the table. `OK` means confirmed, `MISMATCH` means the preset is wrong (fix the value, e.g.
   a folder name or port), `UNTESTED` means it needs your judgement. The script prints the exact
   `verified = true` edits for `src/posta_mcp/presets/bogazici.toml`: the `imap` / `smtp` tables of
   the `[[variants]]` entry matching your account type (`student` or `staff`) and `[folders]`.
   Make them by hand, only for what was OK.
7. Do it again with the other account type if you can get one (a staff account for `staff`, a student
   account for `student`); each variant is verified separately. Do not commit anything containing
   your address or output that includes mail content.
8. `[domains]` and `[classification]` cannot be proven by the script. Look at one real mailing-list
   message (`List-Id` header, sender domains) in your mail client and compare with the preset before
   you flip them.
9. If login fails: password expired (change it at `mail.bogazici.edu.tr/password`), wrong login name
   style (try the full address with `POSTA_USERNAME`), or hosts changed after the 2026 Exchange
   upgrade. Fix the preset values the table flags and rerun.

Then run the whole server once through a client (`docs/CLIENTS.md`) with your real account, reading
only, and try `/posta:hoca-ozet`.

## b) Names and OWNER placeholders (done), PyPI name check (open)

Done: the GitHub owner is `kutluege`, the repository is
`https://github.com/kutluege/bogazici_postanesi`, and the registry name is
`io.github.kutluege/posta-mcp`. The placeholders are replaced in `README.md` (mcp-name line),
`server.json`, `manifest.json`, `pyproject.toml` (urls and author "Ege Kutlu", no email) and
`tests/unit/test_packaging.py`. The README is in Turkish; the English copy is `docs/README.en.md`.

Still to do: check that the PyPI package name `posta-mcp` is free (https://pypi.org/project/posta-mcp/).
If you rename the package, change `name` in `pyproject.toml`, `manifest.json`, `server.json`
(`identifier`), the README commands, and `tests/unit/test_packaging.py`.
`io.github.kutluege/...` must match the GitHub account you log in with when publishing to the
registry. Optionally put your name in `LICENSE` instead of "posta-mcp contributors".

## c) Create the public repository and let CI run

CI (`.github/workflows/ci.yml`) has never run. After creating the empty public repository:

```
git remote add origin https://github.com/kutluege/bogazici_postanesi.git
git push -u origin main
```

Watch the first run: `lint` (ruff), `unit` on ubuntu/windows/macos, `integration` (Docker GreenMail,
`POSTA_REQUIRE_GREENMAIL=1`). Likely first-run problems: the pinned action SHAs, the
`greenmail/standalone:2.1.14` image startup, Windows path handling. Fix before releasing.

## d) Release

Order matters: the registry checks the package on PyPI.

1. Bump the version everywhere if needed: `pyproject.toml`, `src/posta_mcp/__init__.py`,
   `manifest.json`, `server.json` (twice: top level and the package). `tests/unit/test_packaging.py`
   fails when they disagree.
2. Build and check locally: `uv build`, then
   `uvx --from dist/posta_mcp-<version>-py3-none-any.whl posta-mcp --version`.
3. **PyPI.** Try TestPyPI first: `uv publish --publish-url https://test.pypi.org/legacy/` with a
   token, then `uv publish`. The README is the PyPI description, so it must contain the
   `mcp-name:` line with the final owner.
4. **MCP Registry** (preview): install `mcp-publisher` from the registry's GitHub releases, then
   ```
   mcp-publisher validate
   mcp-publisher login github
   mcp-publisher publish
   ```
   run in the folder with `server.json`. It checks the `mcp-name` marker in the PyPI description.
5. **MCPB for Claude Desktop.**
   ```
   npx -y @anthropic-ai/mcpb validate manifest.json
   npx -y @anthropic-ai/mcpb pack . dist/posta-mcp-<version>.mcpb
   ```
   Test it by opening it in Claude Desktop on a clean machine, then attach it to a GitHub release
   (`gh release create v<version> dist/posta-mcp-<version>.mcpb --notes ...`). Optionally sign it
   (`mcpb sign`). `.mcpbignore` keeps tests, docs, `.venv` and the like out of the bundle.

Do not publish before the live verification (a) and before CI is green.

## e) Known limitations

- **Boğaziçi values are unverified** until step (a). The 2026 Exchange upgrade may have changed
  capabilities (MOVE, SPECIAL-USE, UIDPLUS); without MOVE and UIDPLUS the move/trash/archive tools
  refuse by design.
- **`pt.` (part-time) and `retired.` accounts** use different hosts (`smtp-pt...`, `imap-retired...`)
  and are not preset variants: set `POSTA_IMAP_HOST` / `POSTA_SMTP_HOST` explicitly. Alumni and
  legacy `@boun.edu.tr` addresses are not tested at all.
- **Claude Desktop elicitation is unconfirmed.** If Desktop cannot show the confirmation, sending
  refuses there (drafts work). The MCPB `uv` server type has not been tried in a real Claude Desktop
  install, including whether Desktop provides its own `uv`.
- **Elicitation hooks** in a client can auto-approve sends; the server cannot detect that.
- Only password authentication over IMAP/SMTP; no OAuth. Servers that require OAuth are not supported.
- Mail classification (`faculty`, `student`, `list`, `standard`) is heuristic; a professor writing
  from a personal address is `external`, and a staff member counts as `faculty`.
- Profile keyword matching understands Turkish case, diacritics and common suffixes, not full
  morphology.
- Real-server behaviour (rate limits, connection limits, message size) is unknown; the server opens
  one connection per operation.
- GreenMail has no STARTTLS and no SPECIAL-USE, so those paths are covered by unit tests with fakes
  only. The Boğaziçi SMTP (587/STARTTLS) is the first real STARTTLS path; watch it in step (a).
