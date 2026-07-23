"""Encrypted local database.

Uses SQLCipher when the `sqlcipher3` module is available (install extra
`nodary[sqlcipher]`), keyed from the OS keychain. Falls back to plain SQLite
with a loud warning so development and tests work everywhere; the fallback is
recorded in schema_meta so the UI can surface it.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import os
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA_VERSION = "1"

try:
    import sqlcipher3  # type: ignore

    HAVE_SQLCIPHER = True
except ImportError:
    sqlcipher3 = None
    HAVE_SQLCIPHER = False


def default_db_path() -> Path:
    env = os.environ.get("NODARY_DB")
    if env:
        return Path(env)
    home = Path.home() / ".nodary"
    return home / "nodary.db"


def _load_schema() -> str:
    return (
        importlib.resources.files("nodary.storage").joinpath("schema.sql").read_text()
    )


def connect(path: Path | str, key: str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the nodary database.

    `key` is the hex key for SQLCipher; ignored (with a warning) when
    SQLCipher is unavailable.
    """
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)

    # check_same_thread=False: the UI serves reads from Flask worker threads.
    # CPython's sqlite3 is built with SQLITE_THREADSAFE (serialized), so a
    # shared connection is safe.
    if HAVE_SQLCIPHER and key:
        conn = sqlcipher3.connect(str(path), check_same_thread=False)
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        # each dbapi module only accepts its own Row/Cursor types
        conn.row_factory = sqlcipher3.dbapi2.Row
        encryption = "sqlcipher"
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        encryption = "none"
        if key and not HAVE_SQLCIPHER:
            print(
                "WARNING: sqlcipher3 not installed; database is NOT encrypted "
                "at rest. Install with: uv sync --extra sqlcipher",
                file=sys.stderr,
            )

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_load_schema())
    _migrate(conn)
    _set_meta_default(conn, "schema_version", SCHEMA_VERSION)
    _set_meta_default(conn, "encryption", encryption)
    _set_meta_default(conn, "created_at", str(int(time.time())))
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place migrations for databases created by older schemas.
    `CREATE TABLE IF NOT EXISTS` never updates existing tables, so
    constraint changes must be applied by rebuilding the table."""
    accounts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if accounts_sql and "mail_store" not in accounts_sql["sql"]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            BEGIN;
            CREATE TABLE accounts_migrated (
              id          INTEGER PRIMARY KEY,
              email       TEXT NOT NULL UNIQUE,
              imap_host   TEXT NOT NULL,
              imap_port   INTEGER NOT NULL DEFAULT 993,
              auth_method TEXT NOT NULL CHECK
                (auth_method IN ('oauth2','app_password','mail_store')),
              created_at  INTEGER NOT NULL
            );
            INSERT INTO accounts_migrated
              SELECT id, email, imap_host, imap_port, auth_method, created_at
              FROM accounts;
            DROP TABLE accounts;
            ALTER TABLE accounts_migrated RENAME TO accounts;
            COMMIT;
            """
        )
        conn.execute("PRAGMA foreign_keys = ON")


def _set_meta_default(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def open_default() -> sqlite3.Connection:
    from .keys import get_or_create_db_key

    return connect(default_db_path(), get_or_create_db_key())
