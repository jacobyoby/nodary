"""Migration 002 — widen accounts.auth_method CHECK and add last_error.

The original accounts table only allowed ('oauth2','app_password').
This migration rebuilds the table to add 'mail_store' and adds the
last_error column used for sync-health surfacing in the dashboard.

Idempotent: skips the rebuild if 'mail_store' is already in the CHECK
constraint and the last_error column already exists.
"""

from __future__ import annotations

import sqlite3

from . import register_migration


@register_migration(version=2, name="widen_auth_method_check")
def apply(conn: sqlite3.Connection) -> None:
    # Widen the CHECK constraint if needed.
    accounts_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if accounts_sql_row and "mail_store" not in accounts_sql_row["sql"]:
        # Each statement runs inside the caller's explicit transaction.
        # executescript would COMMIT and break the transaction, so we use
        # individual execute calls.
        conn.execute(
            """CREATE TABLE accounts_migrated (
              id          INTEGER PRIMARY KEY,
              email       TEXT NOT NULL UNIQUE,
              imap_host   TEXT NOT NULL,
              imap_port   INTEGER NOT NULL DEFAULT 993,
              auth_method TEXT NOT NULL CHECK
                (auth_method IN ('oauth2','app_password','mail_store')),
              created_at  INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO accounts_migrated
              SELECT id, email, imap_host, imap_port, auth_method, created_at
              FROM accounts"""
        )
        conn.execute("DROP TABLE accounts")
        conn.execute("ALTER TABLE accounts_migrated RENAME TO accounts")

    # Add last_error column if not present.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "last_error" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN last_error TEXT")
