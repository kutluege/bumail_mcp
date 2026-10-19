"""Parse raw RFC 5322 bytes into ``ParsedMessage``; robust header/charset decoding.

Turkish mail in the wild is a mess of UTF-8, ISO-8859-9, Windows-1254, wrong charset labels
and raw 8-bit headers.  Nothing here may raise on bad input and nothing here logs content.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import re
from collections.abc import Iterator
from email import message_from_bytes, policy
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from .models import Attachment, ParsedMessage, normalize_flags

# --------------------------------------------------------------------------- charsets

# ISO-8859-9 is decoded as Windows-1254 (a strict superset; this is what browsers do).
_ALIASES = {
    "iso-8859-9": "cp1254",
    "iso8859-9": "cp1254",
    "iso_8859-9": "cp1254",
    "iso-8859_9": "cp1254",
    "iso-ir-148": "cp1254",
    "latin5": "cp1254",
    "latin-5": "cp1254",
    "l5": "cp1254",
    "windows-1254": "cp1254",
    "cp1254": "cp1254",
    "x-cp1254": "cp1254",
    "ms-turk": "cp1254",
    "ms_turk": "cp1254",
    "x-mac-turkish": "mac_turkish",
    "macturkish": "mac_turkish",
    "mac-turkish": "mac_turkish",
    "utf8": "utf-8",
    "utf-8": "utf-8",
}
_WESTERN = {
    "us-ascii",
    "ascii",
    "iso-8859-1",
    "iso8859-1",
    "latin1",
    "latin-1",
    "l1",
    "windows-1252",
    "cp1252",
    "x-user-defined",
}
_BAD_CODECS = {
    "unicode_escape",
    "raw_unicode_escape",
    "unicode-escape",
    "raw-unicode-escape",
    "idna",
    "punycode",
    "undefined",
    "string_escape",
    "mbcs",
    "oem",
}
# Bytes that are ı İ ş Ş ğ Ğ in ISO-8859-9/1254 but ý Ý þ Þ ð Ð in Latin-1 (a common mislabel).
_TURKISH_HINT = re.compile(rb"[\xd0\xdd\xde\xf0\xfd\xfe]")
_FALLBACKS = ("cp1254", "iso-8859-9")  # iso-8859-9 maps every byte, so this always ends here


def _try(data: bytes, codec: str) -> str | None:
    try:
        return data.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_bytes(data: bytes, charset: str | None = None) -> str:
    """Decode ``data`` using a (possibly wrong or missing) declared charset; never raises.

    Order: declared exotic charset (strict) -> UTF-8 -> Turkish hint for Latin-1 labels ->
    declared charset -> windows-1254 -> iso-8859-9.
    """
    if not data:
        return ""
    name = (charset or "").strip().strip("\"'").lower().split("*")[0]
    codec = _ALIASES.get(name, name)
    if codec in _BAD_CODECS or not re.fullmatch(r"[a-z0-9_.:+-]*", codec):
        codec = ""
    if codec and codec != "utf-8" and codec != "cp1254" and codec not in _WESTERN:
        text = _try(data, codec)  # shift_jis, koi8-r, utf-16 ...: trust the label
        if text is not None:
            return text
    text = _try(data, "utf-8")  # also catches UTF-8 mislabelled as a single-byte charset
    if text is not None:
        return text
    if codec in _WESTERN and _TURKISH_HINT.search(data):
        codec = "cp1254"
    for c in (codec, *_FALLBACKS):
        if c and (text := _try(data, c)) is not None:
            return text
    return data.decode("latin-1", errors="replace")  # unreachable safety net


# --------------------------------------------------------------------------- text hygiene

# Zero-width, bidi-override and Unicode "tag" characters: invisible to a human, readable by a
# model (ASCII smuggling / spoofing), never needed in Turkish or English mail.
_INVISIBLE = re.compile(
    "[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text)


def truncate_text(text: str, max_chars: int) -> str:
    """Cut ``text`` to ``max_chars`` and append a truncation notice."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[... truncated: showing {max_chars} of {len(text)} characters ...]"


# --------------------------------------------------------------------------- headers

_ENCODED_WORD = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?\s]*)\?=")
_QP_BYTE = re.compile(rb"=([0-9A-Fa-f]{2})")
_MSGID = re.compile(r"<[^<>\s]+>")


def _header_text(value: str | bytes | Message | None) -> str:
    """Normalise a raw header value to ``str`` (8-bit bytes decoded with the fallback chain)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = decode_bytes(value)
    else:
        text = str(value)
        if any("\udc80" <= ch <= "\udcff" for ch in text):  # surrogateescape'd raw bytes
            text = decode_bytes(text.encode("utf-8", "surrogateescape"))
    return re.sub(r"\r?\n[ \t]+", " ", text)  # unfold


def _word_bytes(enc: str, payload: str) -> bytes:
    if enc in "bB":
        try:
            return base64.b64decode(payload + "=" * (-len(payload) % 4))
        except (binascii.Error, ValueError):
            return payload.encode("latin-1", "replace")
    q = payload.replace("_", " ").encode("latin-1", "replace")
    return _QP_BYTE.sub(lambda m: bytes([int(m.group(1), 16)]), q)


def _decode_encoded_words(text: str) -> str:
    out: list[str] = []
    group: tuple[str, bytearray] | None = None  # adjacent same-charset words decode together

    def flush() -> None:
        nonlocal group
        if group:
            out.append(decode_bytes(bytes(group[1]), group[0]))
            group = None

    pos = 0
    for m in _ENCODED_WORD.finditer(text):
        gap = text[pos : m.start()]
        charset = m.group(1).split("*")[0].lower()
        raw = _word_bytes(m.group(2), m.group(3))
        if group and not gap.strip():  # whitespace between two encoded words is dropped
            if group[0] == charset:
                group[1].extend(raw)
                pos = m.end()
                continue
            flush()
        else:
            flush()
            out.append(gap)
        group = (charset, bytearray(raw))
        pos = m.end()
    flush()
    out.append(text[pos:])
    return "".join(out)


def decode_header_value(value: str | bytes | None) -> str:
    """Decode a header value: RFC 2047 B/Q words (mixed with plain text), raw 8-bit bytes in
    any plausible Turkish charset, unfolding; control and invisible characters removed."""
    text = _decode_encoded_words(_header_text(value))
    return _CONTROL.sub(" ", strip_invisible(text)).strip()


_ADDR_SPECIALS = re.compile(r'[()<>@,;:\\".\[\]]')


def format_address(name: str, addr: str) -> str:
    """``Name <addr>`` with the name quoted when needed; ``addr`` alone without a name."""
    if not name:
        return addr
    if _ADDR_SPECIALS.search(name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>" if addr else name


def parse_addresses(value: str | bytes | list[Any] | None) -> list[tuple[str, str]]:
    """Header value (or list of values) -> ``[(decoded_name, addr)]``, skipping empties."""
    values = value if isinstance(value, list) else [value]
    texts = [_header_text(v) for v in values if v]
    out = []
    for name, addr in getaddresses(texts) if texts else []:
        addr = _CONTROL.sub("", strip_invisible(addr)).strip()
        if addr:
            out.append((decode_header_value(name), addr))
    return out


def _raw_headers(msg: Message) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in msg.raw_items():
        headers.setdefault(name.lower(), str(value))
    return headers


def parse_header_bytes(data: bytes) -> Message:
    return BytesHeaderParser(policy=policy.compat32).parsebytes(data)


def _iso_date(value: str) -> str | None:
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def header_fields(msg: Message) -> dict[str, Any]:
    """Summary fields (all except uid/folder/flags/size/has_attachments) from message headers."""
    h = _raw_headers(msg)
    senders = parse_addresses(h.get("from"))
    name, addr = senders[0] if senders else ("", "")
    ids = _MSGID.findall(_header_text(h.get("message-id")))
    return {
        "from_name": name,
        "from_addr": addr,
        "to": [format_address(n, a) for n, a in parse_addresses(h.get("to"))],
        "cc": [format_address(n, a) for n, a in parse_addresses(h.get("cc"))],
        "subject": decode_header_value(h.get("subject")),
        "date": _iso_date(_header_text(h.get("date"))),
        "list_id": decode_header_value(h.get("list-id")) or None,
        "message_id": ids[0] if ids else None,
    }


# --------------------------------------------------------------------------- HTML -> text

_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_DROP = {"script", "style", "head", "template", "title"}
_PARA = {
    "p",
    "ul",
    "ol",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "section",
    "article",
    "dl",
    "form",
}
_LINE = {"div", "tr", "li", "hr", "header", "footer", "dt", "dd", "center"}  # newline around
_HIDDEN_STYLE = re.compile(
    r"display:none|visibility:hidden|mso-hide:all|font-size:0(px|pt|em|rem|%)?(;|$)"
    r"|opacity:0(\.0*)?(;|$)|(left|top|text-indent):-\d{4,}"
)


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for key, val in attrs:
        if key == "hidden":
            return True
        if key == "style" and _HIDDEN_STYLE.search(re.sub(r"\s+", "", (val or "").lower())):
            return True
    return False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.nl = 2  # trailing newlines already emitted (start of text counts as a break)
        self.stack: list[tuple[str, bool]] = []  # (tag, hides content)
        self.anchors: list[tuple[str, int]] = []

    @property
    def _hidden(self) -> bool:
        return any(hides for _, hides in self.stack)

    def _break(self, tag: str) -> None:
        want = 2 if tag in _PARA else 1 if tag in _LINE else 0
        if want > self.nl:
            self.chunks.append("\n" * (want - self.nl))
            self.nl = want

    def _emit(self, text: str) -> None:
        self.chunks.append(text)
        if text.strip():
            self.nl = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":  # an unclosed <head> ends here
            for i, (t, _) in enumerate(self.stack):
                if t == "head":
                    del self.stack[i:]
                    break
        was_hidden = self._hidden
        if tag not in _VOID:
            self.stack.append((tag, tag in _DROP or _is_hidden(attrs)))
        if was_hidden or self._hidden or (tag in _VOID and _is_hidden(attrs)):
            return
        if tag == "br":
            self.chunks.append("\n")
            self.nl += 1
        self._break(tag)
        if tag == "li":
            self._emit("- ")
        elif tag in ("td", "th"):
            self._emit(" ")
        elif tag == "a":
            href = re.sub(r"\s+", "", dict(attrs).get("href") or "")
            self.anchors.append((href, len(self.chunks)))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            return
        was_hidden = self._hidden
        del self.stack[idx:]
        if was_hidden:
            return
        if tag == "a" and self.anchors:
            href, start = self.anchors.pop()
            shown = "".join(self.chunks[start:]).strip()
            if (
                re.match(r"(https?://|mailto:)", href, re.IGNORECASE)
                and href.removeprefix("mailto:") not in shown
            ):
                self._emit(f" ({href})" if shown else f"({href})")
        self._break(tag)

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip() or self.nl == 0:
            self._emit(text)


def html_to_text(html: str) -> str:
    """Readable text from HTML: scripts/styles/head and visually hidden elements are dropped,
    links become ``text (url)``, whitespace is collapsed."""
    parser = _TextExtractor()
    with contextlib.suppress(Exception):  # html.parser is lenient; never fail a whole message
        parser.feed(html)
        parser.close()
    text = strip_invisible("".join(parser.chunks))
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------------------- parts

_META_CHARSET = re.compile(rb"charset\s*=\s*[\"']?([\w:.-]+)", re.IGNORECASE)
_TEXT_EXT = (".txt", ".csv", ".tsv", ".md", ".log", ".json", ".xml", ".html", ".htm", ".ics")


def _leaves(msg: Message) -> Iterator[Message]:
    payload = msg.get_payload()
    if msg.get_content_maintype() == "multipart" and isinstance(payload, list):
        for part in payload:
            yield from _leaves(part)
    else:
        yield msg


def _filename(part: Message) -> str:
    value = part.get_param("filename", header="content-disposition")
    if value is None:
        value = part.get_param("name")
    if value is None:
        return ""
    if isinstance(value, tuple):  # RFC 2231: (charset, language, percent-decoded-as-latin1)
        charset, _, val = value
        try:
            name = decode_bytes(val.encode("latin-1"), charset)
        except UnicodeEncodeError:
            name = val
    else:
        name = decode_header_value(value)
    name = _CONTROL.sub(" ", strip_invisible(name)).replace("\\", "/").rsplit("/", 1)[-1]
    return name.strip()


def _payload(part: Message) -> bytes:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        try:
            return inner[0].as_bytes() if isinstance(inner, list) and inner else b""
        except Exception:  # noqa: BLE001
            return b""
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def _decode_text_part(part: Message, data: bytes) -> str:
    charset = part.get_content_charset()
    if not charset and part.get_content_type() == "text/html":
        m = _META_CHARSET.search(data[:2048])
        charset = m.group(1).decode("ascii") if m else None
    return decode_bytes(data, charset)


def _is_flat_multipart(part: Message) -> bool:
    """A multipart without a usable boundary: the parser leaves its payload as one string."""
    return part.get_content_maintype() == "multipart" and not isinstance(part.get_payload(), list)


def _is_body(part: Message, filename: str) -> bool:
    return (
        (part.get_content_type() in ("text/plain", "text/html") or _is_flat_multipart(part))
        and part.get_content_disposition() != "attachment"
        and not filename
    )


def _attachment_parts(msg: Message) -> list[tuple[Message, str]]:
    parts = []
    for leaf in _leaves(msg):
        filename = _filename(leaf)
        if not _is_body(leaf, filename):
            parts.append((leaf, filename))
    return parts


def _render_body_part(part: Message) -> str:
    text = _decode_text_part(part, _payload(part))
    if part.get_content_type() == "text/html":
        return html_to_text(text)
    return strip_invisible(text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _readable_parts(node: Message) -> list[str]:
    """Body texts in reading order. Alternatives of one content collapse to one: text/plain if it
    has content, else the last (richest) non-empty one. Every other multipart (mixed, related, ...)
    keeps all its body parts, e.g. an HTML-only bulletin plus a mailing list's text/plain footer."""
    payload = node.get_payload()
    if node.get_content_maintype() == "multipart" and isinstance(payload, list):
        rendered = [(part, _readable_parts(part)) for part in payload]
        if node.get_content_subtype() == "alternative":
            plain = [t for p, t in rendered if p.get_content_type() == "text/plain" and any(t)]
            filled = [t for _, t in rendered if any(t)]
            return plain[0] if plain else (filled[-1] if filled else [])
        return [t for _, texts in rendered for t in texts]
    return [_render_body_part(node)] if _is_body(node, _filename(node)) else []


def parse_message(raw: bytes, uid: int, folder: str, flags: Any = ()) -> ParsedMessage:
    """Parse a full RFC 5322 message.  Within an alternative, prefers text/plain; standalone HTML
    parts (not an alternative of a plain part) are rendered and kept in reading order."""
    msg = message_from_bytes(raw, policy=policy.compat32)
    attachments = [
        Attachment(
            filename=name or f"attachment-{i}",
            content_type=part.get_content_type(),
            size=len(_payload(part)),
            index=i,
        )
        for i, (part, name) in enumerate(_attachment_parts(msg))
    ]
    h = _raw_headers(msg)
    return ParsedMessage(
        uid=uid,
        folder=folder,
        flags=normalize_flags(flags),
        size=len(raw),
        has_attachments=bool(attachments),
        reply_to=[format_address(n, a) for n, a in parse_addresses(h.get("reply-to"))],
        in_reply_to=next(iter(_MSGID.findall(_header_text(h.get("in-reply-to")))), None),
        references=_MSGID.findall(_header_text(h.get("references"))),
        text_body="\n\n".join(p for p in _readable_parts(msg) if p),
        attachments=attachments,
        **header_fields(msg),
    )


def _is_text_like(content_type: str, filename: str) -> bool:
    if content_type.startswith("text/") or content_type.endswith(("+json", "+xml")):
        return True
    if content_type in ("application/json", "application/xml", "application/csv"):
        return True
    return content_type == "application/octet-stream" and filename.lower().endswith(_TEXT_EXT)


def get_attachment_text(raw: bytes, index: int, max_chars: int = 20000) -> str:
    """Decoded text of attachment ``index`` (as numbered in ``ParsedMessage.attachments``).
    Only text-like parts are supported; anything else raises ``ValueError``."""
    parts = _attachment_parts(message_from_bytes(raw, policy=policy.compat32))
    if not 0 <= index < len(parts):
        raise ValueError(f"No attachment with index {index} (message has {len(parts)}).")
    part, filename = parts[index]
    if not _is_text_like(part.get_content_type(), filename):
        # The content type is sender-controlled, so it is deliberately not echoed.
        raise ValueError(f"Attachment {index} is not a text-like file (type omitted).")
    text = _decode_text_part(part, _payload(part))
    if part.get_content_type() == "text/html" or filename.lower().endswith((".html", ".htm")):
        text = html_to_text(text)  # drops scripts and hidden text, as for HTML bodies
    text = strip_invisible(text.replace("\r\n", "\n").replace("\r", "\n"))
    return truncate_text(text, max_chars)


__all__ = [
    "decode_bytes",
    "decode_header_value",
    "format_address",
    "get_attachment_text",
    "header_fields",
    "html_to_text",
    "parse_addresses",
    "parse_header_bytes",
    "parse_message",
    "strip_invisible",
    "truncate_text",
]
