"""In-memory IMAP transport speaking the same shapes as imapclient:
BODYSTRUCTURE tuples, header bytes, section fetches. Lets sync tests run the
real engine end-to-end with zero network."""

from __future__ import annotations

from email.message import EmailMessage


def _leaf_bs(part: EmailMessage) -> tuple:
    payload = part.get_payload(decode=True) or b""
    params: tuple = ()
    charset = part.get_content_charset()
    if charset:
        params = (b"CHARSET", charset.upper().encode())
    fields: list = [
        part.get_content_maintype().upper().encode(),
        part.get_content_subtype().upper().encode(),
        params or None,
        None,
        None,
        (part.get("Content-Transfer-Encoding") or "7bit").upper().encode(),
        len(payload),
    ]
    if part.get_content_maintype() == "text":
        fields.append(payload.count(b"\n"))
    disposition = part.get_content_disposition()
    if disposition:
        filename = part.get_filename()
        fields.append(
            (
                disposition.upper().encode(),
                (b"FILENAME", filename.encode()) if filename else None,
            )
        )
    return tuple(fields)


def _bodystructure(part: EmailMessage) -> tuple:
    if part.is_multipart():
        children = [_bodystructure(p) for p in part.get_payload()]
        return (children, part.get_content_subtype().upper().encode())
    return _leaf_bs(part)


def _sections(part: EmailMessage, prefix: str = "") -> dict[str, bytes]:
    """Map IMAP section numbers to on-the-wire (still-encoded) payloads."""
    out: dict[str, bytes] = {}
    if part.is_multipart():
        for i, sub in enumerate(part.get_payload(), 1):
            sec = f"{prefix}{i}"
            if sub.is_multipart():
                out.update(_sections(sub, prefix=f"{sec}."))
            else:
                out[sec] = sub.get_payload(decode=False).encode(
                    "ascii", "surrogateescape"
                )
    else:
        out[prefix + "1" if not prefix else prefix.rstrip(".")] = part.get_payload(
            decode=False
        ).encode("ascii", "surrogateescape")
    return out


class FakeFolder:
    def __init__(self, uidvalidity: int = 1):
        self.uidvalidity = uidvalidity
        self.next_uid = 1
        self.messages: dict[int, EmailMessage] = {}

    def add(self, msg: EmailMessage) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = msg
        return uid


class FakeTransport:
    def __init__(self):
        self.folders = {"INBOX": FakeFolder(), "Sent": FakeFolder()}
        self._selected: FakeFolder | None = None
        self.meta_fetches = 0

    # -- test helpers ------------------------------------------------------
    def add(self, folder: str, msg: EmailMessage) -> int:
        return self.folders[folder].add(msg)

    def bump_uidvalidity(self, folder: str) -> None:
        f = self.folders[folder]
        old = f.messages
        f.uidvalidity += 1
        f.next_uid = 1
        f.messages = {}
        for msg in old.values():  # same mail, renumbered UIDs
            f.add(msg)

    # -- Transport interface ------------------------------------------------
    def list_sync_folders(self):
        return [("INBOX", "inbox"), ("Sent", "sent")]

    def select_readonly(self, name: str):
        self._selected = self.folders[name]
        return {
            "uidvalidity": self._selected.uidvalidity,
            "uidnext": self._selected.next_uid,
        }

    def new_uids(self, after_uid: int):
        return sorted(u for u in self._selected.messages if u > after_uid)

    def fetch_meta(self, uids):
        self.meta_fetches += len(uids)
        out = {}
        for uid in uids:
            msg = self._selected.messages.get(uid)
            if msg is None:
                continue
            raw = msg.as_bytes()
            out[uid] = {
                "header": raw,  # headersonly parse ignores the body
                "size": len(raw),
                "bodystructure": _bodystructure(msg),
            }
        return out

    def fetch_part(self, uid: int, section: str) -> bytes:
        msg = self._selected.messages.get(uid)
        if msg is None:
            return b""
        return _sections(msg).get(section, b"")
