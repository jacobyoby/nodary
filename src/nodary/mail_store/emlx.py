"""Parser for Apple Mail .emlx files.

Format (undocumented, stable since 10.4):
    line 1:  ASCII byte count N, newline-terminated
    next N bytes:  the raw RFC822 message
    remainder:  an XML plist of Mail-internal metadata (flags, dates)

`.partial.emlx` files carry the same structure but with large MIME parts
stripped and stored as sidecar files; the headers are always intact, which
is all the feature pipeline needs.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class EmlxError(ValueError):
    """Raised when a file does not follow the emlx framing."""


@dataclass
class EmlxMessage:
    rfc822: bytes  # raw message (may be truncated body for .partial.emlx)
    plist: dict[str, Any]  # Mail-internal metadata; {} if unparseable
    partial: bool  # True when parsed from a .partial.emlx


def read_emlx(path: Path | str) -> EmlxMessage:
    path = Path(path)
    data = path.read_bytes()

    nl = data.find(b"\n")
    if nl < 1:
        raise EmlxError(f"{path.name}: missing byte-count line")
    try:
        count = int(data[:nl].strip())
    except ValueError as e:
        raise EmlxError(f"{path.name}: bad byte-count line") from e

    start = nl + 1
    if count < 0 or start + count > len(data):
        raise EmlxError(f"{path.name}: byte count {count} exceeds file size")
    rfc822 = data[start : start + count]

    meta: dict[str, Any] = {}
    tail = data[start + count :].lstrip()
    if tail:
        try:
            meta = plistlib.loads(tail)
        except Exception:
            meta = {}  # metadata is best-effort; the message is what matters

    return EmlxMessage(
        rfc822=rfc822,
        plist=meta,
        partial=path.name.endswith(".partial.emlx"),
    )
