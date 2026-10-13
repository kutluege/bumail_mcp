"""Build outgoing messages: UTF-8, RFC 2047 headers, threading, strict header-injection checks.

Bcc handling (important for the SMTP layer): ``build_message`` keeps the ``Bcc`` header so a
draft stored via IMAP APPEND still shows it (``message_bytes``).  What goes on the wire must be
``send_bytes(msg)`` (no ``Bcc`` header) with the envelope recipients ``envelope_recipients(msg)``
(To + Cc + Bcc) passed separately, e.g. ``smtp.sendmail(sender, envelope_recipients(msg),
send_bytes(msg))``.  Never use ``smtplib.send_message`` on a message that still has Bcc.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Sequence
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from .mime import decode_header_value, format_address, parse_addresses
from .models import ParsedMessage

_BAD_CHARS = re.compile(r"[\x00\r\n]")
_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|ynt|aw|sv)(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_MAX_REFERENCES = 20


def _check_text(value: str, what: str) -> str:
    if not isinstance(value, str) or _BAD_CHARS.search(value):
        raise ValueError(f"{what} must be a string without CR, LF or NUL characters")
    return value


def parse_address(value: str) -> Address:
    """Validate one ``addr`` or ``Name <addr>`` string; raise ``ValueError`` otherwise."""
    _check_text(value, "address")
    if ("<" in value) != (">" in value):
        raise ValueError(f"unbalanced angle brackets in address: {value!r}")
    pairs = getaddresses([value])
    if len(pairs) != 1 or not pairs[0][1]:
        raise ValueError(f"not a single valid email address: {value!r}")
    name, addr = pairs[0]
    if addr.count("@") != 1 or re.search(r"[\s<>,;()\[\]\"\\]", addr):
        raise ValueError(f"invalid email address: {addr!r}")
    local, domain = addr.split("@")
    if not local or not domain or not local.isascii():
        raise ValueError(f"invalid email address: {addr!r}")
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ValueError(f"invalid email address: {addr!r}")
    try:
        domain = domain.encode("idna").decode("ascii")  # IDN domains go out as punycode
    except UnicodeError as exc:
        raise ValueError(f"invalid email address domain: {addr!r}") from exc
    return Address(
        display_name=decode_header_value(name) if name else "", username=local, domain=domain
    )


def _addresses(value: str | Iterable[str] | None) -> list[Address]:
    if value is None:
        return []
    return [parse_address(v) for v in ([value] if isinstance(value, str) else value)]


def _message_ids(value: str | Iterable[str] | None, what: str) -> list[str]:
    ids: list[str] = []
    for item in [value] if isinstance(value, str) else (value or []):
        _check_text(item, what)
        found = _ID.findall(item)
        if not found:
            raise ValueError(f"{what} must contain a <message-id>")
        ids += found
    return ids


def build_message(
    from_addr: str,
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    in_reply_to: str | None = None,
    references: str | Sequence[str] | None = None,
) -> EmailMessage:
    """Compose a UTF-8 plain-text message.  Any CR/LF/NUL in an address, name or subject (or a
    malformed address) raises ``ValueError``.  Bcc is kept as a header; see module docstring."""
    sender = parse_address(from_addr)
    to_list, cc_list, bcc_list = _addresses(to), _addresses(cc), _addresses(bcc)
    if not (to_list or cc_list or bcc_list):
        raise ValueError("at least one recipient is required")
    _check_text(subject, "subject")
    if not isinstance(body, str) or "\x00" in body:
        raise ValueError("body must be a string without NUL characters")
    reply_ids = _message_ids(in_reply_to, "in_reply_to")
    ref_ids = _message_ids(references, "references")

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    for header, addrs in (("To", to_list), ("Cc", cc_list), ("Bcc", bcc_list)):
        if addrs:
            msg[header] = addrs
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.domain)
    if reply_ids:
        msg["In-Reply-To"] = reply_ids[0]
    if ref_ids:
        msg["References"] = " ".join(ref_ids)
    msg.set_content(body, charset="utf-8")
    return msg


def reply_subject(subject: str) -> str:
    """``Re: `` prefix without stacking (also folds Turkish ``Ynt:``)."""
    rest = _REPLY_PREFIX.sub("", subject).strip()
    return f"Re: {rest}" if rest else "Re:"


def _addr_key(formatted: str) -> str:
    return next((a.lower() for _, a in parse_addresses(formatted)), formatted.lower())


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def build_reply(
    original: ParsedMessage,
    from_addr: str,
    body: str,
    reply_all: bool = False,
    *,
    max_quote_chars: int = 10000,
) -> EmailMessage:
    """Reply to ``original``: goes to Reply-To (else From); ``reply_all`` also Cc's the other
    To/Cc recipients, never ``from_addr`` itself.  Sets In-Reply-To/References and quotes the
    original text."""
    me = _addr_key(from_addr)
    sender = format_address(original.from_name, original.from_addr)
    primary = original.reply_to or ([sender] if original.from_addr else [])
    if primary and all(_addr_key(p) == me for p in primary):  # replying to our own message
        primary = list(original.to)
    seen = {me}
    to: list[str] = []
    for p in primary:
        if _addr_key(p) not in seen:
            seen.add(_addr_key(p))
            to.append(p)
    cc: list[str] = []
    if reply_all:
        for p in (*original.to, *original.cc):
            if _addr_key(p) not in seen:
                seen.add(_addr_key(p))
                cc.append(p)
    if not to and cc:
        to, cc = cc, []

    refs = [*original.references]
    if original.message_id:
        refs.append(original.message_id)
    refs = list(dict.fromkeys(refs))
    if len(refs) > _MAX_REFERENCES:
        refs = [refs[0], *refs[-(_MAX_REFERENCES - 1) :]]

    quoted = original.body[:max_quote_chars]
    if len(original.body) > max_quote_chars:
        quoted += "\n[...]"
    who = sender or "the sender"
    text = f"{body.rstrip()}\n\nOn {original.date or 'an earlier date'}, {who} wrote:\n{_quote(quoted)}\n"
    return build_message(
        from_addr,
        to,
        reply_subject(original.subject),
        text,
        cc=cc,
        in_reply_to=original.message_id,
        references=refs or None,
    )


def envelope_recipients(msg: EmailMessage) -> list[str]:
    """Bare addresses of To + Cc + Bcc (de-duplicated): the SMTP envelope, kept apart from headers."""
    values = [str(v) for h in ("To", "Cc", "Bcc") for v in msg.get_all(h, [])]
    seen: dict[str, str] = {}
    for _, addr in getaddresses(values):
        if addr:
            seen.setdefault(addr.lower(), addr)
    return list(seen.values())


def send_bytes(msg: EmailMessage) -> bytes:
    """Wire bytes for SMTP DATA: the message without any Bcc header (``msg`` is not modified)."""
    clean = copy.deepcopy(msg)
    del clean["Bcc"]
    return clean.as_bytes()


def message_bytes(msg: EmailMessage) -> bytes:
    """Bytes for draft storage (IMAP APPEND): includes Bcc.  Never send these."""
    return msg.as_bytes()
