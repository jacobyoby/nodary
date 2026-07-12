"""Ingest orchestration.

One code path serves live sync, test fixtures, and full rebuild:
facts are inserted once; derived state (profiles, tiers, scores) is applied
by replayable functions, so `rebuild()` can regenerate every profile and
score from the fact tables in sent_at order.

Scoring happens against the sender's baseline as it stood BEFORE the message
being scored — the snapshot is taken first, then the profile is updated.
"""

from __future__ import annotations

import sqlite3

from .feature_extraction.profiles import (
    credit_reply,
    insert_message,
    load_snapshot,
    resolve_thread,
    update_domain_incoming,
    update_profile_incoming,
    upsert_sender,
)
from .feature_extraction.records import AttachmentInfo, MessageRecord
from .scoring.engine import score_message
from .scoring.tiers import compute_tier, store_tier


def ingest_message(
    conn: sqlite3.Connection, folder_id: int, uid: int, record: MessageRecord
) -> int:
    """Persist one new message and apply all derived updates + scoring."""
    thread_id, depth = resolve_thread(conn, record)
    if record.direction == "in":
        sender_id = upsert_sender(conn, record.from_email_norm, record.sent_at)
        msg_row_id = insert_message(
            conn, folder_id, uid, record, sender_id, thread_id, depth
        )
        _apply_incoming(conn, msg_row_id, record, sender_id, thread_id, depth)
    else:
        msg_row_id = insert_message(
            conn, folder_id, uid, record, None, thread_id, depth
        )
        recipient_ids = []
        for addr in dict.fromkeys(record.recipient_addrs_norm):
            sid = upsert_sender(conn, addr, record.sent_at)
            conn.execute(
                "INSERT OR IGNORE INTO message_recipients (message_id, sender_id)"
                " VALUES (?,?)",
                (msg_row_id, sid),
            )
            recipient_ids.append(sid)
        _apply_outgoing(conn, record, thread_id, recipient_ids)
    return msg_row_id


def _sender_seen_in_thread_before(
    conn: sqlite3.Connection, thread_id: int, sender_id: int, sent_at: int
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM messages WHERE thread_id = ? AND sender_id = ?"
            " AND direction = 'in' AND sent_at < ? LIMIT 1",
            (thread_id, sender_id, sent_at),
        ).fetchone()
        is not None
    )


def _apply_incoming(
    conn: sqlite3.Connection,
    msg_row_id: int,
    record: MessageRecord,
    sender_id: int,
    thread_id: int,
    depth: int,
) -> None:
    snap = load_snapshot(conn, sender_id)
    tier = compute_tier(conn, snap)
    score_message(conn, msg_row_id, record, snap, tier)

    thread_is_new = not _sender_seen_in_thread_before(
        conn, thread_id, sender_id, record.sent_at
    )
    update_profile_incoming(conn, sender_id, record, thread_is_new, depth)
    update_domain_incoming(
        conn, snap.reg_domain, record.sent_at, new_sender=snap.n_messages == 0
    )
    store_tier(conn, sender_id, compute_tier(conn, load_snapshot(conn, sender_id)))


def _apply_outgoing(
    conn: sqlite3.Connection,
    record: MessageRecord,
    thread_id: int,
    recipient_ids: list[int],
) -> None:
    thread_sender_ids = {
        r["sender_id"]
        for r in conn.execute(
            "SELECT DISTINCT sender_id FROM messages WHERE thread_id = ?"
            " AND direction = 'in' AND sender_id IS NOT NULL AND sent_at < ?",
            (thread_id, record.sent_at),
        )
    }
    credited = []
    for sid in thread_sender_ids:
        if credit_reply(conn, thread_id, sid, initiated=False):
            credited.append(sid)
    for sid in recipient_ids:
        if sid not in thread_sender_ids and credit_reply(
            conn, thread_id, sid, initiated=True
        ):
            credited.append(sid)
    for sid in credited:
        store_tier(conn, sid, compute_tier(conn, load_snapshot(conn, sid)))


# ------------------------------------------------------------------ rebuild --

_DERIVED_TABLES = [
    "message_score_features",
    "message_scores",
    "thread_reply_credits",
    "sender_replyto_addrs",
    "sender_link_domains",
    "sender_attachment_types",
    "sender_display_names",
    "sender_profiles",
    "domain_profiles",
]


def _record_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> MessageRecord:
    attachments = [
        AttachmentInfo(r["mime_type"], r["extension"] or "", r["size_bytes"])
        for r in conn.execute(
            "SELECT mime_type, extension, size_bytes FROM message_attachments"
            " WHERE message_id = ?",
            (row["id"],),
        )
    ]
    link_domains = {
        r["reg_domain"]: r["n"]
        for r in conn.execute(
            "SELECT reg_domain, n FROM message_link_domains WHERE message_id = ?",
            (row["id"],),
        )
    }
    return MessageRecord(
        direction=row["direction"],
        message_id=row["message_id"],
        from_email_norm=row["from_email_norm"],
        from_display_name=row["from_display_name"],
        reply_to_email_norm=row["reply_to_email_norm"],
        to_me_directly=bool(row["to_me_directly"]),
        n_recipients=row["n_recipients"],
        sent_at=row["sent_at"],
        sent_hour_local=row["sent_hour_local"],
        sent_dow_local=row["sent_dow_local"],
        size_bytes=row["size_bytes"],
        attachments=attachments,
        link_domains=link_domains,
        links_extracted=bool(row["links_extracted"]),
        in_reply_to="<replay>" if row["is_reply"] else None,
        auth_spf=row["auth_spf"],
        auth_dkim=row["auth_dkim"],
        auth_dmarc=row["auth_dmarc"],
    )


def rebuild(conn: sqlite3.Connection) -> int:
    """Regenerate all profiles, tiers, and scores from the fact tables,
    replaying messages in sent_at order. Returns messages processed."""
    for table in _DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")

    n = 0
    rows = conn.execute("SELECT * FROM messages ORDER BY sent_at, id").fetchall()
    for row in rows:
        record = _record_from_row(conn, row)
        if row["direction"] == "in":
            _apply_incoming(
                conn,
                row["id"],
                record,
                row["sender_id"],
                row["thread_id"],
                row["thread_depth"],
            )
        else:
            recipient_ids = [
                r["sender_id"]
                for r in conn.execute(
                    "SELECT sender_id FROM message_recipients WHERE message_id = ?",
                    (row["id"],),
                )
            ]
            _apply_outgoing(conn, record, row["thread_id"], recipient_ids)
        n += 1
    conn.commit()
    return n
