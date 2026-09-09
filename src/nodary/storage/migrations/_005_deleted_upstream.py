"""Migration 005 — add deleted_upstream column to messages.

Marks locally-retained messages that no longer exist on the server.
Nodary never deletes mailbox contents; deletion reconciliation is
mark-only so behavioral baselines are preserved.

Idempotent: checks ``PRAGMA table_info`` before ``ALTER TABLE``.
"""

from __future__ import annotations

import sqlite3

from . import register_migration


@register_migration(version=5, name="add_deleted_upstream_column")
def apply(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "deleted_upstream" not in cols:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN deleted_upstream INTEGER NOT NULL DEFAULT 0"
        )
