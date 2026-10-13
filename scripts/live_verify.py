"""Live verification of a posta-mcp preset against YOUR real mail server (milestone M6).

Run it yourself, on your own machine, with your own account:

    uv run python scripts/live_verify.py                  # read-only checks (default)
    uv run python scripts/live_verify.py --show-subjects  # also print the newest subject
    uv run python scripts/live_verify.py --append-draft   # also APPEND one test draft to Drafts
    uv run python scripts/live_verify.py --send-self      # also send ONE test mail to yourself

Default checks are read-only: TLS connect and IMAP login, capabilities, folder list and
SPECIAL-USE roles, a BODY.PEEK fetch of one message (confirming \\Seen did not change), and an
SMTP TLS connect and login without sending anything. It never prints the password, message bodies
or (without --show-subjects) subjects. It prints a table of preset fields (OK / MISMATCH /
UNTESTED) and the exact `verified = true` edits to make in the preset file.

It reads the same configuration as the server (config.toml, POSTA_* variables) and takes the
password from POSTA_PASSWORD or the OS keychain (`posta-mcp setup`), else asks with getpass.
Nothing is stored. Stdlib plus the posta_mcp package only.
"""

from __future__ import annotations

import argparse
import email
import getpass
import os
import smtplib
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from email import policy

OK, MISMATCH, UNTESTED, INFO = "OK", "MISMATCH", "UNTESTED", "INFO"
ROLES = ("sent", "drafts", "trash", "archive")
CAPABILITIES = ("MOVE", "UIDPLUS", "SPECIAL-USE", "IDLE")
TEST_SUBJECT = "posta-mcp live_verify test – Türkçe: ğüşıöç İI"
TEST_BODY = (
    "This is a test message created by posta-mcp scripts/live_verify.py.\n"
    "Türkçe karakter testi: ğüşıöç ĞÜŞİÖÇ.\n"
    "It is safe to delete.\n"
)


@dataclass
class Row:
    field: str
    status: str
    detail: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append(Row(name, status, detail))

    def status(self, name: str) -> str:
        return next((r.status for r in self.rows if r.field == name), UNTESTED)


# ---------------------------------------------------------------------------- pure helpers


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="live_verify.py",
        description="Check a posta-mcp preset against your own real mail account. "
        "Read-only unless you pass --append-draft or --send-self.",
    )
    parser.add_argument(
        "--show-subjects",
        action="store_true",
        help="print the subject of the one message that is fetched (default: uid/size/charsets only)",
    )
    parser.add_argument(
        "--append-draft",
        action="store_true",
        help="APPEND one clearly marked test draft to your Drafts folder (never sent)",
    )
    parser.add_argument(
        "--send-self",
        action="store_true",
        help="send ONE test message to your own address only (asks you to retype the address)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="network timeout in seconds")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def confirm_matches(typed: str, address: str) -> bool:
    """The --send-self confirmation: the typed text must equal the account address."""
    return bool(typed.strip()) and typed.strip().casefold() == address.strip().casefold()


def capability_flags(capabilities: Sequence[bytes | str]) -> dict[str, bool]:
    names = {
        (c.decode("ascii", "replace") if isinstance(c, bytes) else str(c)).upper()
        for c in capabilities
    }
    return {cap: cap in names for cap in CAPABILITIES}


def compare_folders(
    folders: Sequence[tuple[str, str | None]], preset: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Compare the server's folders with the preset's names, per role.

    `folders` is [(name, special_use_role_or_None)], `preset` maps role -> configured name.
    Returns role -> (status, detail). OK when the preset name exists and agrees with SPECIAL-USE
    (if the server advertises one); MISMATCH when the name is missing or SPECIAL-USE names a
    different folder.
    """
    names = {name for name, _ in folders}
    special = {role: name for name, role in folders if role}
    out: dict[str, tuple[str, str]] = {}
    for role in ROLES:
        want = preset.get(role, "")
        flagged = special.get(role)
        if flagged and flagged != want:
            out[role] = (
                MISMATCH,
                f"preset '{want}' but server marks '{flagged}' as \\{role.title()}",
            )
        elif want in names:
            how = "SPECIAL-USE agrees" if flagged else "exists (server has no SPECIAL-USE for it)"
            out[role] = (OK, f"'{want}' {how}")
        else:
            out[role] = (MISMATCH, f"'{want}' not found (folders: {len(names)})")
    return out


def charset_names(raw: bytes) -> list[str]:
    """Declared charsets of every part of a raw message (never any content)."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    seen: list[str] = []
    for part in msg.walk():
        charset = part.get_content_charset()
        if charset and charset.lower() not in seen:
            seen.append(charset.lower())
    return seen


def format_report(report: Report) -> str:
    width = max([len(r.field) for r in report.rows] + [5])
    lines = [f"{'FIELD'.ljust(width)}  {'STATUS'.ljust(8)}  DETAIL", "-" * (width + 40)]
    lines += [f"{r.field.ljust(width)}  {r.status.ljust(8)}  {r.detail}" for r in report.rows]
    return "\n".join(lines)


def edit_instructions(preset: str, variant: str, rows: Sequence[Row]) -> list[str]:
    """Which `verified = true` edits are justified by the OK rows."""
    status = {r.field: r.status for r in rows}

    def all_ok(prefix: str) -> bool:
        group = [s for f, s in status.items() if f.startswith(prefix)]
        return bool(group) and all(s == OK for s in group)

    if preset == "generic":
        return ["The generic preset has no unverified fields; there is nothing to mark verified."]
    path = f"presets/{preset}.toml"
    edits: list[str] = []
    for proto in ("imap", "smtp"):
        if all_ok(f"{proto}."):
            table = (
                f'[variants.{proto}] of the [[variants]] entry named "{variant}"'
                if variant
                else f"[{proto}]"
            )
            edits.append(f"{path}: in {table} set  verified = true")
    if all_ok("folders."):
        edits.append(f"{path}: in [folders] set  verified = true")
    if any(s == MISMATCH for f, s in status.items() if f.startswith("folders.")):
        edits.append(
            f"{path}: correct the mismatching names in [folders] to what the server uses, rerun, "
            "then set verified = true"
        )
    if status.get("login") == OK:
        edits.append(f"{path}: remove the '# unverified' comment on username_style (login worked)")
    if not edits:
        edits.append("No preset section was fully confirmed; do not set any verified = true.")
    edits.append(
        "[domains] and [classification] cannot be confirmed by this script: check a real list "
        "message (List-Id) and sender domains by hand before marking them verified."
    )
    return edits


# ---------------------------------------------------------------------------- live checks


def _note_failure(report: Report, names: Sequence[str], exc: BaseException) -> None:
    for name in names:
        report.add(name, UNTESTED, f"{type(exc).__name__}: {str(exc)[:120]}")


def check_imap(config, report: Report, args: argparse.Namespace) -> None:
    """IMAP login, capabilities, folders, one PEEK fetch, optional draft append."""
    from posta_mcp.imap import Mailbox, MailboxError, SearchCriteria
    from posta_mcp.logs import redact
    from posta_mcp.mime import parse_message

    imap_fields = ["imap.host", "imap.port", "imap.security"]
    try:
        mailbox = Mailbox(config, timeout=args.timeout)
        mailbox.__enter__()
    except MailboxError as exc:
        text = redact(str(exc))
        if "rejected the credentials" in text:  # TCP + TLS fine, so host/port/security are right
            for name in imap_fields:
                report.add(name, OK, "TLS connect works")
            report.add("login", MISMATCH, f"login name '{config.login}' or password rejected")
        else:
            for name in imap_fields:
                report.add(name, MISMATCH, text[:140])
            report.add("login", UNTESTED, "connection failed")
        _note_failure(report, [f"folders.{r}" for r in ROLES], RuntimeError("no IMAP session"))
        return
    for name in imap_fields:
        report.add(name, OK, "TLS verified, login accepted")
    report.add("login", OK, f"login name '{config.login}' accepted")
    try:
        caps = capability_flags(mailbox.client.capabilities())
        for cap, present in caps.items():
            report.add(
                f"cap.{cap}",
                INFO,
                "advertised" if present else "not advertised",
            )
        if not caps["MOVE"] and not caps["UIDPLUS"]:
            report.notes.append(
                "Server has neither MOVE nor UIDPLUS: move/trash/archive tools will refuse."
            )

        folders = mailbox.list_folders()
        result = compare_folders(
            [(f.name, f.special_use) for f in folders],
            {role: getattr(config.folders, role) for role in ROLES},
        )
        for role, (status, detail) in result.items():
            report.add(f"folders.{role}", status, detail)
        report.notes.append(f"{len(folders)} folders listed (names are not printed).")

        _peek_check(mailbox, report, args, SearchCriteria, parse_message)

        if args.append_draft:
            _append_draft(mailbox, config, report)
    finally:
        mailbox.__exit__(None, None, None)


def _flags(mailbox, folder: str, uid: int) -> set[str]:
    mailbox.client.select_folder(folder, readonly=True)  # EXAMINE: cannot change flags
    data = mailbox.client.fetch([uid], ["FLAGS"])
    return {f.decode() if isinstance(f, bytes) else str(f) for f in data[uid][b"FLAGS"]}


def _peek_check(mailbox, report: Report, args, SearchCriteria, parse_message) -> None:
    folder = "INBOX"
    try:
        unseen = mailbox.search(folder, SearchCriteria(unseen=True), limit=1)
        newest = unseen or mailbox.search(folder, SearchCriteria(), limit=1)
        if not newest:
            report.add("peek.fetch", UNTESTED, "INBOX is empty")
            return
        summary = newest[0]
        before = _flags(mailbox, folder, summary.uid)
        raw = mailbox.fetch_raw(folder, summary.uid)  # BODY.PEEK[]
        after = _flags(mailbox, folder, summary.uid)
        parsed = parse_message(raw, summary.uid, folder, sorted(after))
        charsets = charset_names(raw)
        garbled = "�" in parsed.body or "�" in parsed.subject
        print(
            f"  fetched uid={summary.uid} size={len(raw)} bytes charsets={charsets or ['(none)']}"
            + (f" subject={parsed.subject!r}" if args.show_subjects else "")
        )
        if "\\Seen" in before:
            report.add("peek.seen", UNTESTED, "message was already read; rerun with an unread one")
        elif "\\Seen" in after:
            report.add("peek.seen", MISMATCH, "\\Seen was SET by a PEEK fetch: do not trust reads")
        else:
            report.add("peek.seen", OK, "BODY.PEEK left \\Seen unchanged")
        report.add("peek.fetch", OK, f"uid {summary.uid}, {len(raw)} bytes")
        report.add(
            "decoding",
            MISMATCH if garbled else OK,
            "replacement characters in decoded text"
            if garbled
            else f"decoded cleanly ({', '.join(charsets) or 'no charset'})",
        )
    except Exception as exc:  # noqa: BLE001 - report, never crash the whole run
        report.add("peek.fetch", UNTESTED, f"{type(exc).__name__}: {str(exc)[:120]}")


def _append_draft(mailbox, config, report: Report) -> None:
    from posta_mcp.compose import build_message, message_bytes

    try:
        msg = build_message(config.email, config.email, TEST_SUBJECT, TEST_BODY)
        folder = mailbox.ensure_special_folder("drafts")
        uid = mailbox.append(folder, message_bytes(msg), ("\\Draft",))
        stored = mailbox.fetch_raw(folder, uid) if uid else b""
        round_trip = "güş".encode() in stored or b"UTF-8" in stored.upper()
        report.add(
            "append.draft",
            OK,
            f"saved in '{folder}' (uid {uid if uid else 'unknown'}); delete it by hand afterwards"
            + ("" if round_trip or not uid else "; UTF-8 round trip not confirmed"),
        )
    except Exception as exc:  # noqa: BLE001
        report.add("append.draft", MISMATCH, f"{type(exc).__name__}: {str(exc)[:120]}")


def smtp_connect(config, password: str, timeout: float):
    """Open a verified SMTP session (ssl or starttls) and log in. Caller must quit()."""
    from posta_mcp.tls import make_ssl_context

    context = make_ssl_context(config.ca_file)
    smtp_cfg = config.smtp
    if smtp_cfg.security == "ssl":
        client = smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, context=context, timeout=timeout)
        client.ehlo()
    else:
        client = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=timeout)
        client.ehlo()
        if not client.has_extn("starttls"):
            client.close()
            raise smtplib.SMTPException("server does not offer STARTTLS")
        client.starttls(context=context)
        client.ehlo()
    try:
        client.login(config.login, password)
    except BaseException:
        client.close()
        raise
    return client


def check_smtp(config, password: str, report: Report, args: argparse.Namespace) -> None:
    from posta_mcp.logs import redact

    fields = ["smtp.host", "smtp.port", "smtp.security"]
    try:
        client = smtp_connect(config, password, args.timeout)
    except smtplib.SMTPAuthenticationError:
        for name in fields:
            report.add(name, OK, "TLS connect works")
        report.add(
            "smtp.login", MISMATCH, "SMTP login rejected (same credentials as IMAP expected)"
        )
        return
    except Exception as exc:  # noqa: BLE001
        for name in fields:
            report.add(name, MISMATCH, redact(f"{type(exc).__name__}: {exc}", password)[:140])
        report.add("smtp.login", UNTESTED, "connection failed")
        return
    try:
        for name in fields:
            report.add(name, OK, "TLS verified, login accepted (nothing sent)")
        report.add("smtp.login", OK, "same credentials as IMAP")
        extras = sorted(
            k.upper()
            for k in client.esmtp_features
            if k.upper() in {"SIZE", "8BITMIME", "SMTPUTF8", "AUTH"}
        )
        report.notes.append(f"SMTP extensions of interest: {', '.join(extras) or 'none'}")
        if args.send_self:
            _send_self(client, config, report, args)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            client.close()


def _send_self(client, config, report: Report, args: argparse.Namespace) -> None:
    from posta_mcp.compose import build_message, envelope_recipients, send_bytes

    typed = input(f"Type your address again ({config.email}) to send ONE test mail to yourself: ")
    if not confirm_matches(typed, config.email):
        report.add("send.self", UNTESTED, "confirmation did not match; nothing sent")
        return
    marker = f"{TEST_SUBJECT} [{int(time.time())}]"
    msg = build_message(config.email, config.email, marker, TEST_BODY)
    recipients = envelope_recipients(msg)
    assert [r.casefold() for r in recipients] == [config.email.casefold()], "self-send only"
    try:
        client.sendmail(config.email, recipients, send_bytes(msg))
    except Exception as exc:  # noqa: BLE001
        report.add("send.self", MISMATCH, f"{type(exc).__name__}: {str(exc)[:120]}")
        return
    report.add("send.self", OK, "SMTP accepted the message; look for it in your inbox")


# ---------------------------------------------------------------------------- main


def obtain_password(config) -> str:
    from posta_mcp.credentials import CredentialsError, get_password

    try:
        return get_password(config.login)
    except CredentialsError as exc:
        print(f"  {exc}")
        password = getpass.getpass(f"Password for '{config.login}' (not shown, not stored): ")
        if not password:
            raise SystemExit("A password is required.") from None
        from posta_mcp.logs import register_secret

        register_secret(password)
        os.environ["POSTA_PASSWORD"] = password  # this process only; lets Mailbox find it
        return password


def untested_rows(report: Report, config) -> None:
    for section in ("domains", "classification"):
        model = getattr(config, section)
        for name in type(model).model_fields:
            if name != "verified":
                report.add(f"{section}.{name}", UNTESTED, "needs real mail; check by hand")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from posta_mcp.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    if config.missing():
        print(
            f"Missing settings: {', '.join(config.missing())}. Run `posta-mcp setup`.",
            file=sys.stderr,
        )
        return 2

    print(
        f"posta-mcp live verification: preset={config.preset} account type={config.variant or '-'}"
    )
    print(
        f"  IMAP {config.imap.host}:{config.imap.port} ({config.imap.security}), "
        f"SMTP {config.smtp.host}:{config.smtp.port} ({config.smtp.security}), login '{config.login}'"
    )
    print(
        "  Read-only unless --append-draft / --send-self were given. The password is never printed."
    )
    password = obtain_password(config)

    report = Report()
    print("\n[1/2] IMAP ...")
    check_imap(config, report, args)
    print("[2/2] SMTP ...")
    check_smtp(config, password, report, args)
    untested_rows(report, config)

    print("\n" + format_report(report))
    for note in report.notes:
        print(f"note: {note}")
    print("\nEdits to make (only if you agree with the results above):")
    for line in edit_instructions(config.preset, config.variant, report.rows):
        print(f"  - {line}")
    failed = any(r.status == MISMATCH for r in report.rows if not r.field.startswith("cap."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
