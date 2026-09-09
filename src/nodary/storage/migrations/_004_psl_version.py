"""Migration 004 — record PSL snapshot version in schema_meta.

Stores the current PSL identity so the application can detect when
``tldextract`` upgrades change the bundled suffix list and profiles
should be rebuilt.

Idempotent: ``INSERT OR IGNORE`` leaves an existing row untouched.
"""

from __future__ import annotations

import sqlite3

from ..psl import get_current_psl_identity
from . import register_migration


@register_migration(version=4, name="add_psl_version_to_schema_meta")
def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('psl_version', ?)",
        (get_current_psl_identity(),),
    )
