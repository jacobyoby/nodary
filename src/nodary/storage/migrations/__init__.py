"""Versioned schema migration framework.

Migrations are registered with the ``@register_migration`` decorator and
applied in version order by ``run_migrations``.  Each migration runs
inside a transaction; on failure the transaction is rolled back, the
foreign-key pragma is restored, and a clear error is raised.

To add a new migration:

1. Create ``src/nodary/storage/migrations/_NNN_short_name.py``.
2. Decorate its ``apply(conn)`` with
   ``@register_migration(version=NNN, name="short_name")``.
3. Import the module at the bottom of this ``__init__.py``.
4. Bump ``LATEST_VERSION`` below.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Highest migration version.  Bump when adding a new migration file.
LATEST_VERSION = 3

_REGISTRY: dict[int, tuple[str, callable]] = {}


def register_migration(version: int, name: str):
    """Decorator that registers a migration function.

    The decorated function receives a ``sqlite3.Connection`` and must be
    idempotent (safe to re-run against a database where the change is
    already present).
    """

    def decorator(fn):
        if version in _REGISTRY:
            raise ValueError(
                f"migration version {version} already registered"
                f" as {_REGISTRY[version][0]!r}"
            )
        _REGISTRY[version] = (name, fn)
        return fn

    return decorator


def get_current_version(conn: sqlite3.Connection) -> int:
    """Read ``schema_meta.schema_version``, returning 0 when absent."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def get_pending_migrations(
    current_version: int,
    target_version: int | None = None,
) -> list[tuple[int, str, callable]]:
    """Return ordered list of ``(version, name, apply_fn)`` to apply."""
    target = target_version if target_version is not None else LATEST_VERSION
    return [
        (ver, name, fn)
        for ver, (name, fn) in sorted(_REGISTRY.items())
        if current_version < ver <= target
    ]


def run_migrations(
    conn: sqlite3.Connection,
    target_version: int | None = None,
) -> list[int]:
    """Apply pending migrations in order.

    Returns the list of version numbers that were applied.

    Each migration runs inside an explicit transaction.  If any migration
    fails the transaction is rolled back, the ``foreign_keys`` pragma is
    restored to its pre-migration state, and a ``RuntimeError`` is raised
    with the migration name and version.
    """
    current = get_current_version(conn)
    pending = get_pending_migrations(current, target_version)
    applied: list[int] = []

    # Save FK pragma so we can restore after each migration.
    fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
    fk_was_on = bool(fk_row[0]) if fk_row else False

    for version, name, fn in pending:
        logger.info("applying migration %d: %s", version, name)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            fn(conn)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(version),),
            )
            conn.execute("COMMIT")
            applied.append(version)
        except Exception:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"migration {version} ({name}) failed"
            ) from None
        finally:
            # Restore FK pragma regardless of outcome.
            if fk_was_on:
                conn.execute("PRAGMA foreign_keys = ON")
            else:
                conn.execute("PRAGMA foreign_keys = OFF")

    return applied


# ---------------------------------------------------------------------------
# Import migration modules so they register themselves.
# Add new modules here when creating a migration.
# ---------------------------------------------------------------------------
from . import (  # noqa: E402, F401
    _001_initial,
    _002_auth_method_check,
    _003_noop_example,
)
