"""Locate messages inside Apple Mail's on-disk store.

Mail's `Envelope Index` (SQLite, WAL) indexes every local message; the
message ROWID is also the .emlx filename, giving a stable, monotonically
increasing id that maps cleanly onto nodary's UID-based incremental sync.

Layout facts (verified on V10):
  message file:  <V10>/<acct-uuid>/<path components each + .mbox>/
                 <instance-uuid>/Data/<reversed digits of rowid//1000>/
                 Messages/<rowid>.emlx  (or .partial.emlx)
  Gmail:         Mail stores each message once, under [Gmail]/All Mail;
                 INBOX and Sent Mail are empty locally.

The index is only ever opened read-only (URI mode=ro); WAL allows reading
while Mail itself is writing.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from pathlib import Path

# Folder roles nodary syncs, in candidate order. `sent` folders teach the
# pipeline which messages are outgoing; on Gmail that signal comes from the
# From header instead (All Mail holds sent and received alike).
SENT_NAMES = ("Sent Messages", "[Gmail]/Sent Mail", "Sent Items", "Sent")
INBOX_NAMES = ("[Gmail]/All Mail", "INBOX")


def default_root() -> Path:
    env = os.environ.get("NODARY_MAIL_STORE")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Mail" / "V10"


class MailStore:
    def __init__(self, root: Path | None = None):
        self.root = root or default_root()
        self._conn: sqlite3.Connection | None = None
        self._instance_dirs: dict[Path, Path] = {}

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            db = self.root / "MailData" / "Envelope Index"
            if not db.is_file():
                raise FileNotFoundError(
                    f"{db}: Mail store index not found (is Full Disk Access "
                    "granted, and does this macOS use the V10 layout?)"
                )
            uri = f"file:{urllib.parse.quote(str(db))}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # -- mailbox enumeration -------------------------------------------------

    def _mailboxes(self) -> list[tuple[int, str, str]]:
        """All IMAP mailboxes as (rowid, account_uuid, decoded_path)."""
        out = []
        for r in self.conn.execute(
            "SELECT ROWID, url FROM mailboxes WHERE url LIKE 'imap://%'"
        ):
            parsed = urllib.parse.urlparse(r["url"])
            path = urllib.parse.unquote(parsed.path.lstrip("/"))
            out.append((r["ROWID"], parsed.netloc, path))
        return out

    def detect_account_uuid(self, email: str) -> str | None:
        """Match a nodary account to a store account by counting messages
        the address itself sent within each store account."""
        email = email.lower()
        best: tuple[int, str] | None = None
        for uuid in {u for _, u, _ in self._mailboxes()}:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM messages m"
                " JOIN mailboxes mb ON m.mailbox = mb.ROWID"
                " JOIN addresses a ON m.sender = a.ROWID"
                " WHERE mb.url LIKE ? AND lower(a.address) = ? AND m.deleted = 0",
                (f"imap://{uuid}/%", email),
            ).fetchone()[0]
            if n and (best is None or n > best[0]):
                best = (n, uuid)
        return best[1] if best else None

    def sync_folders(self, account_uuid: str) -> list[tuple[str, str]]:
        """(folder_path, role) pairs worth syncing for one account: the
        first non-empty inbox-like folder plus any non-empty sent folder."""
        counts: dict[str, int] = {}
        rowids: dict[str, int] = {}
        for rowid, uuid, path in self._mailboxes():
            if uuid != account_uuid:
                continue
            rowids[path] = rowid
            counts[path] = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE mailbox = ? AND deleted = 0",
                (rowid,),
            ).fetchone()[0]
        out: list[tuple[str, str]] = []
        for name in INBOX_NAMES:
            if counts.get(name):
                out.append((name, "inbox"))
                break
        for name in SENT_NAMES:
            if counts.get(name):
                out.append((name, "sent"))
                break
        return out

    def mailbox_rowid(self, account_uuid: str, path: str) -> int:
        quoted = urllib.parse.quote(path)
        row = self.conn.execute(
            "SELECT ROWID FROM mailboxes WHERE url IN (?, ?)",
            (
                f"imap://{account_uuid}/{quoted}",
                f"imap://{account_uuid}/{path}",
            ),
        ).fetchone()
        if row is None:
            raise KeyError(f"no mailbox {path!r} in account {account_uuid}")
        return row["ROWID"]

    # -- messages ------------------------------------------------------------

    def new_rowids(self, mailbox_rowid: int, after: int) -> list[int]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT ROWID FROM messages"
                " WHERE mailbox = ? AND deleted = 0 AND ROWID > ?"
                " ORDER BY ROWID",
                (mailbox_rowid, after),
            )
        ]

    def max_rowid(self, mailbox_rowid: int) -> int:
        row = self.conn.execute(
            "SELECT MAX(ROWID) FROM messages WHERE mailbox = ?", (mailbox_rowid,)
        ).fetchone()
        return row[0] or 0

    def message_path(
        self, account_uuid: str, folder_path: str, rowid: int
    ) -> Path | None:
        """Resolve a message ROWID to its .emlx file, or None if absent."""
        mbox = self.root / account_uuid
        for comp in folder_path.split("/"):
            mbox = mbox / f"{comp}.mbox"
        inst = self._instance_dir(mbox)
        if inst is None:
            return None
        digits = str(rowid // 1000)
        sub = inst / "Data"
        if digits != "0":
            for d in reversed(digits):
                sub = sub / d
        for suffix in (".emlx", ".partial.emlx"):
            p = sub / "Messages" / f"{rowid}{suffix}"
            if p.is_file():
                return p
        return None

    def _instance_dir(self, mbox: Path) -> Path | None:
        cached = self._instance_dirs.get(mbox)
        if cached is not None:
            return cached
        if not mbox.is_dir():
            return None
        for child in mbox.iterdir():
            if child.is_dir() and (child / "Data").is_dir():
                self._instance_dirs[mbox] = child
                return child
        return None
