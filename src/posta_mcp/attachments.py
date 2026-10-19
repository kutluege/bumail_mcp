"""Files from this computer attached to outgoing mail (posta_gonder, taslak_olustur, yanit_taslagi_olustur).

Attaching a local file is a way for data to leave the machine, so paths are checked strictly:

* absolute paths only (``~`` is expanded), resolved through symlinks, regular files only;
* no network paths (``\\\\server\\share``, ``//server/share``): on Windows merely opening one
  sends the user's NTLM credentials to that server, and drafts need no confirmation;
* nothing inside a hidden directory or itself hidden (``~/.ssh``, ``~/.aws``, ``.env`` ...),
  judged on the resolved path so a symlink cannot smuggle one in.  On Windows a folder with the
  hidden attribute counts too (``AppData``, ``ProgramData``: browser profiles, app tokens);
* at most ``MAX_FILES`` files and ``MAX_TOTAL_BYTES`` in total (base64 adds about a third, so
  the message stays under the usual 25 MB provider limit).

Errors are ``ValueError`` with short messages naming only the file name, never its content.
The send confirmation shows each file's full path (see ``tools/send.py``).
"""

from __future__ import annotations

import mimetypes
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 10
MAX_TOTAL_BYTES = 18 * 1024 * 1024
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class FileAttachment:
    path: str  # resolved absolute path, shown in the send confirmation
    filename: str
    maintype: str
    subtype: str
    data: bytes


def _resolve(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or _BAD_CHARS.search(raw):
        raise ValueError("attachment paths must be non-empty strings without control characters")
    path = Path(raw).expanduser()
    if str(path).startswith(("\\\\", "//")):  # checked before anything touches the file system
        raise ValueError(f"network paths are not allowed for attachments: {path.name!r}")
    if not path.is_absolute():
        raise ValueError(f"attachment path must be absolute: {path.name!r}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"attachment not found: {path.name!r}") from None
    if any(part.startswith(".") for part in resolved.parts[1:]) or _windows_hidden(resolved):
        raise ValueError(
            f"refusing to attach {resolved.name!r}: it is hidden or inside a hidden folder "
            "(such folders often hold keys and passwords)"
        )
    if not resolved.is_file():
        raise ValueError(f"attachment is not a regular file: {resolved.name!r}")
    return resolved


def _attributes(path: Path) -> int:
    """Windows file attributes of ``path`` (0 elsewhere)."""
    return getattr(path.stat(), "st_file_attributes", 0)


def _windows_hidden(path: Path) -> bool:
    # The drive root itself is normally hidden+system, so it is skipped.
    return any(_attributes(p) & stat.FILE_ATTRIBUTE_HIDDEN for p in (path, *path.parents[:-1]))


def load(paths: Sequence[str]) -> list[FileAttachment]:
    """Read and type each file in ``paths``; raise ``ValueError`` if any is not allowed."""
    if len(paths) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} attachments per message")
    resolved = [_resolve(p) for p in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("the same file is attached more than once")
    _check_total(sum(p.stat().st_size for p in resolved))  # before reading anything
    out: list[FileAttachment] = []
    for path in resolved:
        ctype, encoding = mimetypes.guess_type(path.name)
        if ctype is None or encoding is not None:  # e.g. .tar.gz: send the bytes as they are
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError(f"attachment could not be read: {path.name!r}") from None
        out.append(FileAttachment(str(path), path.name, maintype, subtype, data))
    _check_total(sum(len(a.data) for a in out))  # a file may have grown since stat()
    return out


def _check_total(total: int) -> None:
    if total > MAX_TOTAL_BYTES:
        mb = 1024 * 1024
        raise ValueError(
            f"attachments total {total / mb:.1f} MB; the limit is {MAX_TOTAL_BYTES // mb} MB"
        )
