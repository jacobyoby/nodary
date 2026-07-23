"""Transport over the local Apple Mail store.

Implements the same protocol as ImapTransport, so sync.py is unchanged.
Because the raw message is on disk, parts are served already decoded: the
synthesized BODYSTRUCTURE declares an empty content-transfer-encoding and
fetch_part returns decoded bytes, which sync passes through unchanged.

UIDVALIDITY is constant: Envelope Index ROWIDs come from AUTOINCREMENT and
are never reused, so the high-water-mark contract holds for the life of the
store.
"""

from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser

from .emlx import EmlxError, read_emlx
from .store import MailStore

_parser = BytesParser(policy=policy.default)


def _leaf_tuple(part: Message) -> tuple:
    params: list[str] = []
    charset = part.get_content_charset()
    if charset:
        params += ["charset", charset]
    filename = part.get_filename() or ""
    if filename:
        params += ["name", filename]
    payload = part.get_payload(decode=True) or b""
    disposition = None
    if part.get_content_disposition() == "attachment":
        disposition = ("attachment", ("filename", filename))
    return (
        part.get_content_maintype(),
        part.get_content_subtype(),
        tuple(params) or None,
        None,
        None,
        "",  # encoding: parts are served decoded
        len(payload),
        disposition,
    )


def _build(part: Message, section: str, parts_out: dict[str, bytes]) -> tuple:
    """Synthesize a BODYSTRUCTURE tuple bodystructure.walk() understands,
    mirroring its section numbering, while collecting decoded payloads."""
    if part.is_multipart() and part.get_content_maintype() == "multipart":
        children = []
        for i, child in enumerate(part.get_payload(), 1):
            children.append(_build(child, f"{section}{i}.", parts_out))
        return (children, part.get_content_subtype())
    leaf_section = section.rstrip(".") or "1"
    if part.get_content_maintype() != "message":
        parts_out[leaf_section] = part.get_payload(decode=True) or b""
    return _leaf_tuple(part)


class MailStoreTransport:
    def __init__(self, store: MailStore, account_uuid: str):
        self.store = store
        self.account_uuid = account_uuid
        self.skipped = 0  # indexed messages whose .emlx was missing/unreadable
        self._folder: str | None = None
        self._mailbox_rowid: int | None = None
        self._parts: dict[int, dict[str, bytes]] = {}

    # -- Transport protocol --------------------------------------------------

    def list_sync_folders(self) -> list[tuple[str, str]]:
        return self.store.sync_folders(self.account_uuid)

    def select_readonly(self, name: str) -> dict[str, int]:
        self._folder = name
        self._mailbox_rowid = self.store.mailbox_rowid(self.account_uuid, name)
        return {
            # the mailbox ROWID stands in for UIDVALIDITY: if the store is
            # ever recreated (new machine, Mail reset), mailbox rowids change
            # and the standard invalidate-and-refetch path fires — otherwise
            # message rowids could restart below the high-water mark and new
            # mail would be skipped forever
            "uidvalidity": self._mailbox_rowid,
            "uidnext": self.store.max_rowid(self._mailbox_rowid) + 1,
        }

    def new_uids(self, after_uid: int) -> list[int]:
        assert self._mailbox_rowid is not None, "call select_readonly first"
        return self.store.new_rowids(self._mailbox_rowid, after_uid)

    def fetch_meta(self, uids: list[int]) -> dict[int, dict]:
        assert self._folder is not None, "call select_readonly first"
        self._parts.clear()
        out: dict[int, dict] = {}
        for uid in uids:
            path = self.store.message_path(self.account_uuid, self._folder, uid)
            if path is None:
                self.skipped += 1
                continue
            try:
                raw = read_emlx(path).rfc822
                msg = _parser.parsebytes(raw)
                parts: dict[str, bytes] = {}
                bodystructure = _build(msg, "", parts)
            except (EmlxError, OSError):
                self.skipped += 1
                continue
            except Exception:
                # a single malformed message must never abort the sync;
                # skip it and let the high-water mark move past
                self.skipped += 1
                continue
            self._parts[uid] = parts
            out[uid] = {
                "header": _header_bytes(raw),
                "size": len(raw),
                "bodystructure": bodystructure,
            }
        return out

    def fetch_part(self, uid: int, section: str) -> bytes:
        return self._parts.get(uid, {}).get(section, b"")

    def logout(self) -> None:
        pass  # nothing to release; sqlite handle lives on the store


def _header_bytes(raw: bytes) -> bytes:
    """Everything up to the first blank line, whichever line ending wins."""
    ends = [
        (idx, len(sep))
        for sep in (b"\r\n\r\n", b"\n\n")
        if (idx := raw.find(sep)) != -1
    ]
    if not ends:
        return raw
    idx, seplen = min(ends)
    return raw[: idx + seplen]
