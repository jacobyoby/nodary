"""Migration 001 — base schema.

schema.sql creates all tables with CREATE TABLE IF NOT EXISTS, so this
migration is effectively a no-op for databases that already have the
base tables.  It exists to anchor version 1 in the migration history.
"""

from __future__ import annotations

import sqlite3

from . import register_migration


@register_migration(version=1, name="initial_schema")
def apply(conn: sqlite3.Connection) -> None:
    # Tables are created by schema.sql via CREATE TABLE IF NOT EXISTS.
    # This migration is a no-op for the tables themselves; it anchors
    # version 1 so that subsequent migrations have a baseline.
    pass
