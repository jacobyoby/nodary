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

    if HAVE_SQLCIPHER and key:
        conn = sqlcipher3.connect(str(path))
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        encryption = "sqlcipher"
    else:
        conn = sqlite3.connect(str(path))
        encryption = "none"
        if key and not HAVE_SQLCIPHER:
            print(
                "WARNING: sqlcipher3 not installed; database is NOT encrypted "
                "at rest. Install with: uv sync --extra sqlcipher",
                file=sys.stderr,
            )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_load_schema())
    _set_meta_default(conn, "schema_version", SCHEMA_VERSION)
    _set_meta_default(conn, "encryption", encryption)
    _set_meta_default(conn, "created_at", str(int(time.time())))
    conn.commit()
    return conn


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
