"""Migration 006 — add partial unique index for message deduplication.

Guarantees idempotent ingest: (message_id, sent_at, size_bytes) is unique
where message_id IS NOT NULL. Prevents double-insert on forwarded copies
or race conditions; the pipeline can then rely on ON CONFLICT handling.
Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS.
"""

from __future__ import annotations

import sqlite3

from . import register_migration


@register_migration(version=6, name="dedupe_partial_index")
def apply(conn: sqlite3.Connection) -> None:
    # Be tolerant of bare/minimal DBs used in tests (e.g. test_migrations _bare_conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not row or not row[0]:
        return
    sql = row[0]
    # Minimal test table lacks message_id/size_bytes; skip until real schema present
    if "message_id" not in sql or "size_bytes" not in sql:
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe "
        "ON messages(message_id, sent_at, size_bytes) "
        "WHERE message_id IS NOT NULL"
    )
