"""Incremental, UIDVALIDITY-aware sync.

Per folder: keep (uidvalidity, last_seen_uid); fetch only UIDs above the
high-water mark. If the server's UIDVALIDITY changes, the folder's facts are
invalid — wipe and refetch (still headers + structure only, never bodies
beyond bounded text parts for link extraction).
"""

from __future__ import annotations

import base64
import binascii
import quopri
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from ..feature_extraction.extract import (
    MAX_TEXT_SCAN_BYTES,
    parse_auth_results,
    record_from_headers,
)
from ..feature_extraction.normalize import normalize_address
from ..feature_extraction.records import AttachmentInfo
from ..pipeline import ingest_message
from .bodystructure import PartInfo, walk
from .client import FetchFailure, Transport

BATCH_SIZE = 200
TEXT_FETCH_MAX_AGE_DAYS = 90
_parser = BytesParser(policy=policy.default)


@dataclass
class SyncStats:
    new_messages: int = 0
    invalidated_folders: list[str] = field(default_factory=list)
    initial_backfill: bool = False
    server_deleted: int = 0


def reconcile_deleted_uids(
    conn: sqlite3.Connection,
    account_id: int,
    folder_id: int,
    server_uids: set[int],
) -> int:
    """Mark locally-retained messages that no longer exist on the server.

    Rows are never removed — the ``deleted_upstream`` flag is informational
    only, distinguishing "never fetched" from "deleted upstream" while
    preserving all facts for behavioral baselines.
    """
    local_uids = set(
        row[0]
        for row in conn.execute(
            "SELECT uid FROM messages WHERE folder_id=?",
            (folder_id,),
        )
    )
    vanished = local_uids - server_uids
    if vanished:
        placeholders = ",".join("?" * len(vanished))
        conn.execute(
            f"UPDATE messages SET deleted_upstream=1"
            f" WHERE folder_id=? AND uid IN ({placeholders})",
            [folder_id] + list(vanished),
        )
    # Un-mark any that reappeared (re-fetched after being deleted).
    reappeared = local_uids & server_uids
    if reappeared:
        placeholders = ",".join("?" * len(reappeared))
        conn.execute(
            f"UPDATE messages SET deleted_upstream=0"
            f" WHERE folder_id=? AND uid IN ({placeholders})"
            f" AND deleted_upstream=1",
            [folder_id] + list(reappeared),
        )
    return len(vanished)


def _decode_part(data: bytes, encoding: str) -> bytes | None:
    """Decode a transfer-encoded part. Returns None when base64 padding is
    invalid so the caller can mark the message not fully scanned — same
    treatment as an oversized part — instead of pretending the body was
    empty."""
    enc = encoding.lower()
    if enc == "base64":
        try:
            return base64.b64decode(data, validate=False)
        except binascii.Error:
            return None
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
        if decoded is None:
            fully = False
            continue
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
    conn.execute("DELETE FROM skipped_messages WHERE folder_id = ?", (folder_id,))
    conn.execute(
        "UPDATE folders SET last_seen_uid = 0, uidvalidity = NULL WHERE id = ?",
        (folder_id,),
    )


def _record_permanent_skip(
    conn: sqlite3.Connection, folder_id: int, failure: FetchFailure
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO skipped_messages"
        " (folder_id, uid, path, reason, skipped_at) VALUES (?,?,?,?,?)",
        (folder_id, failure.uid, failure.path, failure.reason, int(time.time())),
    )


def _message_date(header_bytes: bytes) -> datetime | None:
    """Parse the Date header from raw header bytes. Returns None on failure."""
    try:
        msg = _parser.parsebytes(header_bytes, headersonly=True)
        dt = parsedate_to_datetime(msg.get("Date", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def sync_folder(
    conn: sqlite3.Connection,
    transport: Transport,
    account_id: int,
    name: str,
    role: str,
    my_addrs: frozenset[str],
    stats: SyncStats,
    *,
    batch_size: int = BATCH_SIZE,
    text_fetch_max_age_days: int | None = None,
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
    conn.execute(
        "UPDATE folders SET uidvalidity = ? WHERE id = ?",
        (server["uidvalidity"], folder_id),
    )

    uids = transport.new_uids(row["last_seen_uid"])
    if row["last_seen_uid"] == 0 and uids:
        stats.initial_backfill = True
    now = datetime.now(UTC)
    for start in range(0, len(uids), batch_size):
        batch = uids[start : start + batch_size]
        fetched = transport.fetch_meta(batch)
        meta = fetched.messages
        permanent = {f.uid: f for f in fetched.permanent_failures}
        # Transient gaps (absent / still-downloading files) stay out of both
        # maps. Permanent corruptions are listed so we can record them and
        # advance the high-water mark instead of stalling the folder.
        missing = [u for u in batch if u not in meta and u not in permanent]
        if missing:
            gap = min(missing)
            batch = [u for u in batch if u < gap]
            permanent = {uid: f for uid, f in permanent.items() if uid < gap}
        for failure in permanent.values():
            _record_permanent_skip(conn, folder_id, failure)
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
            # Age-gated text-part fetch: skip body text retrieval for
            # messages older than the configured threshold. This saves
            # significant time and memory during large first-run backfills
            # where link extraction on old messages provides little value.
            # Identity features are still scored without body text.
            skip_text = False
            if text_fetch_max_age_days is not None:
                msg_date = _message_date(m["header"])
                if msg_date is not None:
                    age_days = (now - msg_date).days
                    if age_days > text_fetch_max_age_days:
                        skip_text = True
            if skip_text:
                link_text, fully = "", False
            else:
                link_text, fully = _gather_text(transport, uid, parts)
            _, from_addr = parseaddr(str(msg.get("From", "")))
            # Self-From alone must not bypass scoring. A genuine self-sent
            # copy lives in the Sent folder or carries a receiving-server
            # Authentication-Results stamp without a DMARC failure. A
            # self-From message with no verdict at all is the spoofing blind
            # spot — classify it incoming so it gets scored.
            auth = parse_auth_results(msg.get("Authentication-Results", "") or "")
            is_self = normalize_address(from_addr) in my_addrs
            if role == "sent" or (is_self and auth and auth.get("dmarc") != "fail"):
                direction = "out"
            else:
                direction = "in"
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
        if batch:
            conn.execute(
                "UPDATE folders SET last_seen_uid = ?, last_synced_at = ? WHERE id = ?",
                (max(batch), int(time.time()), folder_id),
            )
            conn.commit()
        if missing:
            break  # gap: everything from min(missing) on retries next sync

    # Reconcile: mark locally-retained messages that vanished from the server.
    # new_uids(0) returns all server UIDs (UID > 0), cheap for both IMAP
    # (one SEARCH) and the mail store (one SQL query).
    all_server_uids = set(transport.new_uids(0))
    n_deleted = reconcile_deleted_uids(conn, account_id, folder_id, all_server_uids)
    if n_deleted:
        conn.commit()
    stats.server_deleted += n_deleted


def sync_account(
    conn: sqlite3.Connection,
    transport: Transport,
    account_id: int,
    *,
    batch_size: int = BATCH_SIZE,
    text_fetch_max_age_days: int | None = None,
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
        sync_folder(
            conn,
            transport,
            account_id,
            name,
            role,
            my_addrs,
            stats,
            batch_size=batch_size,
            text_fetch_max_age_days=text_fetch_max_age_days,
        )
    return stats
