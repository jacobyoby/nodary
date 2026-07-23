"""Incremental, UIDVALIDITY-aware sync.

Per folder: keep (uidvalidity, last_seen_uid); fetch only UIDs above the
high-water mark. If the server's UIDVALIDITY changes, the folder's facts are
invalid — wipe and refetch (still headers + structure only, never bodies
beyond bounded text parts for link extraction).
"""

from __future__ import annotations

import base64
import quopri
import sqlite3
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from ..feature_extraction.extract import MAX_TEXT_SCAN_BYTES, record_from_headers
from ..feature_extraction.normalize import normalize_address
from ..feature_extraction.records import AttachmentInfo
from ..pipeline import ingest_message
from .bodystructure import PartInfo, walk
from .client import Transport

BATCH_SIZE = 200
_parser = BytesParser(policy=policy.default)


@dataclass
class SyncStats:
    new_messages: int = 0
    invalidated_folders: list[str] = field(default_factory=list)
    initial_backfill: bool = False


def _decode_part(data: bytes, encoding: str) -> bytes:
    enc = encoding.lower()
    if enc == "base64":
        try:
            return base64.b64decode(data, validate=False)
        except Exception:
            return b""
    if enc == "quoted-printable":
        return quopri.decodestring(data)
    return data


def _gather_text(
    transport: Transport, uid: int, parts: list[PartInfo]
) -> tuple[str, bool]:
    """Fetch text/plain + text/html parts for link extraction, bounded per
    part. Returns (text, fully_scanned)."""
    texts: list[str] = []
    fully = True
    for p in parts:
        if (
            p.is_attachment
            or p.maintype != "text"
            or p.subtype not in ("plain", "html")
        ):
            continue
        if p.size is not None and p.size > MAX_TEXT_SCAN_BYTES:
            fully = False
            continue
        raw = transport.fetch_part(uid, p.section)
        if len(raw) > MAX_TEXT_SCAN_BYTES:
            fully = False
            continue
        decoded = _decode_part(raw, p.encoding)
        try:
            texts.append(decoded.decode(p.charset or "utf-8", errors="replace"))
        except LookupError:
            texts.append(decoded.decode("utf-8", errors="replace"))
    return "\n".join(texts), fully


def _folder_id(conn: sqlite3.Connection, account_id: int, name: str, role: str) -> int:
    row = conn.execute(
        "SELECT id FROM folders WHERE account_id = ? AND name = ?",
        (account_id, name),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO folders (account_id, name, role) VALUES (?,?,?)",
        (account_id, name, role),
    )
    return cur.lastrowid


def _invalidate_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    conn.execute("DELETE FROM messages WHERE folder_id = ?", (folder_id,))
    conn.execute(
        "UPDATE folders SET last_seen_uid = 0, uidvalidity = NULL WHERE id = ?",
        (folder_id,),
    )


def sync_folder(
    conn: sqlite3.Connection,
    transport: Transport,
    account_id: int,
    name: str,
    role: str,
    my_addrs: frozenset[str],
    stats: SyncStats,
) -> None:
    folder_id = _folder_id(conn, account_id, name, role)
    server = transport.select_readonly(name)

    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row["uidvalidity"] is not None and row["uidvalidity"] != server["uidvalidity"]:
        _invalidate_folder(conn, folder_id)
        stats.invalidated_folders.append(name)
        row = conn.execute(
            "SELECT * FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
    if row["last_seen_uid"] == 0:
        stats.initial_backfill = True

    conn.execute(
        "UPDATE folders SET uidvalidity = ? WHERE id = ?",
        (server["uidvalidity"], folder_id),
    )

    uids = transport.new_uids(row["last_seen_uid"])
    for start in range(0, len(uids), BATCH_SIZE):
        batch = uids[start : start + BATCH_SIZE]
        meta = transport.fetch_meta(batch)
        for uid in batch:
            m = meta.get(uid)
            if m is None:
                continue
            msg = _parser.parsebytes(m["header"], headersonly=True)
            parts = walk(m["bodystructure"]) if m["bodystructure"] else []
            attachments = [
                AttachmentInfo(p.mime_type, p.filename_ext, p.size)
                for p in parts
                if p.is_attachment
            ]
            link_text, fully = _gather_text(transport, uid, parts)
            _, from_addr = parseaddr(str(msg.get("From", "")))
            direction = (
                "out"
                if role == "sent" or normalize_address(from_addr) in my_addrs
                else "in"
            )
            record = record_from_headers(
                msg,
                direction=direction,
                my_addrs=my_addrs,
                size_bytes=m["size"],
                attachments=attachments,
                link_text=link_text,
                links_extracted=fully,
            )
            if not record.from_email_norm:
                continue  # unparseable From; nothing to attribute
            ingest_message(conn, folder_id, uid, record)
            stats.new_messages += 1
        conn.execute(
            "UPDATE folders SET last_seen_uid = ?, last_synced_at = ? WHERE id = ?",
            (max(batch), int(time.time()), folder_id),
        )
        conn.commit()


def sync_account(
    conn: sqlite3.Connection,
    transport: Transport,
    account_id: int,
) -> SyncStats:
    # normalize both sides: stored identities may predate normalization
    # (e.g. dotted gmail addresses), and the From header is always raw
    my_addrs = frozenset(
        normalize_address(r["email_norm"])
        for r in conn.execute(
            "SELECT email_norm FROM user_identities WHERE account_id = ?",
            (account_id,),
        )
    )
    stats = SyncStats()
    # Sent first: outgoing history must exist before incoming mail is scored,
    # so reply credits and tiers are right during initial backfill. A rebuild
    # after backfill makes ordering exact.
    folders = sorted(
        transport.list_sync_folders(), key=lambda f: 0 if f[1] == "sent" else 1
    )
    for name, role in folders:
        sync_folder(conn, transport, account_id, name, role, my_addrs, stats)
    return stats
