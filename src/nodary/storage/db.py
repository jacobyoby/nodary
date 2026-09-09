"""Encrypted local database.

Uses SQLCipher when the `sqlcipher3` module is available (install extra
`nodary[sqlcipher]`), keyed from the OS keychain. Falls back to plain SQLite
with a loud warning so development and tests work everywhere; the fallback is
recorded in schema_meta so the UI can surface it.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "4"

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
    if key is not None:
        try:
            if len(bytes.fromhex(key)) != 32:
                raise ValueError
        except ValueError as e:
            raise ValueError("database key must be 64 hex characters") from e

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

    if str(path) != ":memory:":
        # owner-only regardless of umask: the plain fallback is plaintext PII
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_load_schema())
    _migrate(conn)
    _set_meta_default(conn, "schema_version", SCHEMA_VERSION)
    # encryption reflects the mode in use right now, not creation time: a DB
    # created before the sqlcipher extra was installed would otherwise report
    # a stale 'none' forever
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('encryption', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (encryption,),
    )
    _set_meta_default(conn, "created_at", str(int(time.time())))

    conn.commit()
    return conn


def get_psl_drift_info(conn: sqlite3.Connection) -> dict:
    """Return PSL drift information for the given connection.

    Returns a dict with keys:
    - ``current``: identity of the currently bundled PSL snapshot
    - ``stored``: identity recorded when profiles were last built
    - ``drift``: ``True`` when the two differ
    """
    from .psl import get_current_psl_identity

    current = get_current_psl_identity()
    stored = get_meta(conn, "psl_version") or current
    return {
        "current": current,
        "stored": stored,
        "drift": stored != current,
    }


def _migrate(conn: sqlite3.Connection) -> None:
    """Run pending schema migrations."""
    from .migrations import run_migrations

    applied = run_migrations(conn)
    if applied:
        logger.info("applied migrations: %s", applied)


def _set_meta_default(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def open_default() -> sqlite3.Connection:
    from .keys import get_or_create_db_key

    return connect(default_db_path(), get_or_create_db_key())
