"""Migration 003 — add index on messages(sent_at) for date-range queries.

Forward-looking additive migration demonstrating the pattern for
contributors.  The index speeds up dashboard date-range filtering.
CREATE INDEX IF NOT EXISTS makes this safely idempotent.
"""

from __future__ import annotations

import sqlite3

from . import register_migration


@register_migration(version=3, name="add_messages_sent_at_index")
def apply(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages(sent_at)")
