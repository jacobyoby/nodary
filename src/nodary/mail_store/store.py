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

# Layout versions we have verified against. Expand as newer layouts are
# tested and confirmed compatible.
SUPPORTED_LAYOUTS = {"V10"}

# Known Apple Mail layout directories, ordered newest first so the detector
# prefers the most recent compatible version when multiple exist.
KNOWN_ROOTS = ["V10", "V9", "V8", "V7", "V6"]


class MailStoreError(OSError):
    """Base error for mail-store problems."""


class MailStoreLayoutError(MailStoreError):
    """Raised when the Apple Mail store layout is missing or unsupported.

    Attributes:
        path: the directory that was probed (if any).
        found_version: the layout version directory found (e.g. "V11"), or
            None when no Mail directory exists at all.
        supported: the set of layout versions this build understands.
    """

    def __init__(
        self,
        path: Path | None,
        found_version: str | None,
        supported: frozenset[str],
        message: str,
    ):
        super().__init__(message)
        self.path = path
        self.found_version = found_version
        self.supported = supported


def detect_mail_store_root(configured_path: str | None = None) -> Path:
    """Detect and validate the Apple Mail store root.

    If *configured_path* is set (via ``NODARY_MAIL_STORE`` or CLI), validate
    it directly — it must exist and contain a known layout directory or
    itself be a layout directory with an Envelope Index.

    Otherwise, probe ``~/Library/Mail/`` for directories matching known
    layouts (V10, V9, …).  Prefer the newest supported layout when
    multiple exist.

    Raises :class:`MailStoreLayoutError` with an actionable message when
    the store cannot be found or the only layout present is unsupported.
    """
    if configured_path:
        return _validate_explicit_path(configured_path)
    return _probe_default()


def _validate_explicit_path(configured_path: str) -> Path:
    """Validate an explicitly configured mail-store path."""
    p = Path(configured_path)

    # The user may point directly at a layout dir (e.g. …/V10) or at the
    # parent Mail directory containing one.
    if _has_envelope_index(p):
        return p

    # Maybe they pointed at the parent (~/Library/Mail)?
    if p.is_dir():
        found = _find_layout_in(p)
        if found is not None:
            return found

    if not p.exists():
        raise MailStoreLayoutError(
            path=p,
            found_version=None,
            supported=frozenset(SUPPORTED_LAYOUTS),
            message=(
                f"configured mail-store path does not exist: {p}\n"
                "set NODARY_MAIL_STORE to a directory containing a supported"
                f" layout ({', '.join(sorted(SUPPORTED_LAYOUTS))})."
            ),
        )
    raise MailStoreLayoutError(
        path=p,
        found_version=None,
        supported=frozenset(SUPPORTED_LAYOUTS),
        message=(
            f"configured mail-store path has no recognised layout: {p}\n"
            "expected a subdirectory named"
            f" {', '.join(KNOWN_ROOTS)} or a layout directory containing"
            " MailData/Envelope Index.\n"
            "set NODARY_MAIL_STORE to a directory containing a supported"
            f" layout ({', '.join(sorted(SUPPORTED_LAYOUTS))})."
        ),
    )


def _probe_default() -> Path:
    """Probe ~/Library/Mail/ for a known layout directory."""
    mail_dir = Path.home() / "Library" / "Mail"
    if not mail_dir.is_dir():
        raise MailStoreLayoutError(
            path=mail_dir,
            found_version=None,
            supported=frozenset(SUPPORTED_LAYOUTS),
            message=(
                f"Apple Mail directory not found: {mail_dir}\n"
                "nodary reads from the local Apple Mail store. Ensure Apple "
                "Mail is set up on this machine and Full Disk Access is "
                "granted to the terminal.\n"
                "if your Mail store lives elsewhere, set NODARY_MAIL_STORE "
                "to the layout directory"
                f" (e.g. ~/Library/Mail/{', '.join(sorted(SUPPORTED_LAYOUTS))})."
            ),
        )

    found = _find_layout_in(mail_dir)
    if found is not None:
        return found

    # No known layout directory — check for unknown ones to give a better
    # error message.
    unknown = sorted(
        d.name
        for d in mail_dir.iterdir()
        if d.is_dir() and d.name.startswith("V") and d.name[1:].isdigit()
    )
    if unknown:
        newest = unknown[-1]  # sorted alphabetically, V11 > V10
        raise MailStoreLayoutError(
            path=mail_dir,
            found_version=newest,
            supported=frozenset(SUPPORTED_LAYOUTS),
            message=(
                f"unsupported Apple Mail layout: {newest} "
                f"(found in {mail_dir})\n"
                f"nodary supports: {', '.join(sorted(SUPPORTED_LAYOUTS))}.\n"
                f"if you believe {newest} is compatible, set "
                f"NODARY_MAIL_STORE to the layout directory and file an "
                f"issue so it can be verified."
            ),
        )
    raise MailStoreLayoutError(
        path=mail_dir,
        found_version=None,
        supported=frozenset(SUPPORTED_LAYOUTS),
        message=(
            f"no recognised Apple Mail layout in {mail_dir}\n"
            f"expected one of: {', '.join(KNOWN_ROOTS)}.\n"
            "if your Mail store uses a different layout, set "
            "NODARY_MAIL_STORE to the layout directory"
            f" (e.g. ~/Library/Mail/{', '.join(sorted(SUPPORTED_LAYOUTS))})."
        ),
    )


def _find_layout_in(parent: Path) -> Path | None:
    """Return the newest supported layout directory inside *parent*."""
    for name in KNOWN_ROOTS:
        candidate = parent / name
        if candidate.is_dir() and name in SUPPORTED_LAYOUTS:
            return candidate
    return None


def _has_envelope_index(p: Path) -> bool:
    return p.is_dir() and (p / "MailData" / "Envelope Index").is_file()


def default_root() -> Path:
    """Resolve the mail-store root, using NODARY_MAIL_STORE if set."""
    env = os.environ.get("NODARY_MAIL_STORE")
    return detect_mail_store_root(configured_path=env)


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
                    f"granted, and does this macOS use a supported layout? "
                    f"supported: {', '.join(sorted(SUPPORTED_LAYOUTS))})"
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
