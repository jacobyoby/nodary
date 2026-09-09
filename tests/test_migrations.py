"""Tests for the versioned schema migration framework."""

from __future__ import annotations

import sqlite3

import pytest

from nodary.storage import db as storage_db
from nodary.storage.migrations import (
    _REGISTRY,
    LATEST_VERSION,
    get_current_version,
    get_pending_migrations,
    register_migration,
    run_migrations,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bare_conn():
    """In-memory conn with schema_meta and accounts — enough for migrations.

    ``isolation_level=None`` puts the connection in autocommit mode so that
    explicit BEGIN / COMMIT / ROLLBACK in the migration runner work without
    interference from Python's implicit-transaction handling.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE accounts (
          id          INTEGER PRIMARY KEY,
          email       TEXT NOT NULL UNIQUE,
          imap_host   TEXT NOT NULL,
          imap_port   INTEGER NOT NULL DEFAULT 993,
          auth_method TEXT NOT NULL
            CHECK (auth_method IN ('oauth2','app_password','mail_store')),
          created_at  INTEGER NOT NULL
        );
        CREATE TABLE folders (
          id             INTEGER PRIMARY KEY,
          account_id     INTEGER NOT NULL REFERENCES accounts(id),
          name           TEXT NOT NULL,
          role           TEXT NOT NULL
            CHECK (role IN ('inbox','sent','archive','other')),
          uidvalidity    INTEGER,
          last_seen_uid  INTEGER NOT NULL DEFAULT 0,
          last_synced_at INTEGER,
          UNIQUE (account_id, name)
        );
        CREATE TABLE messages (
          id         INTEGER PRIMARY KEY,
          folder_id  INTEGER NOT NULL REFERENCES folders(id),
          uid        INTEGER NOT NULL,
          sent_at    INTEGER NOT NULL,
          UNIQUE (folder_id, uid)
        );
        """
    )
    return conn


def _create_v1_db(path):
    """Create a database frozen at schema_version 1 (pre-widening).

    The accounts table uses the old CHECK constraint; all other tables
    match the current schema so that schema.sql's
    ``CREATE TABLE IF NOT EXISTS`` is a harmless no-op when connect()
    opens this database.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Create everything except accounts (which uses the old CHECK).
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '1');

        -- Old accounts: only oauth2 and app_password.
        CREATE TABLE accounts (
          id          INTEGER PRIMARY KEY,
          email       TEXT NOT NULL UNIQUE,
          imap_host   TEXT NOT NULL,
          imap_port   INTEGER NOT NULL DEFAULT 993,
          auth_method TEXT NOT NULL
            CHECK (auth_method IN ('oauth2','app_password')),
          created_at  INTEGER NOT NULL
        );
        """
    )
    conn.close()

    # Re-open and create the remaining tables using the current schema.sql
    # content (minus accounts and schema_meta).  This ensures
    # CREATE TABLE IF NOT EXISTS in connect() is a no-op.
    conn2 = sqlite3.connect(str(path), isolation_level=None)
    conn2.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_identities (
          account_id INTEGER NOT NULL REFERENCES accounts(id),
          email_norm TEXT NOT NULL,
          PRIMARY KEY (account_id, email_norm)
        );
        CREATE TABLE IF NOT EXISTS folders (
          id             INTEGER PRIMARY KEY,
          account_id     INTEGER NOT NULL REFERENCES accounts(id),
          name           TEXT NOT NULL,
          role           TEXT NOT NULL
            CHECK (role IN ('inbox','sent','archive','other')),
          uidvalidity    INTEGER,
          last_seen_uid  INTEGER NOT NULL DEFAULT 0,
          last_synced_at INTEGER,
          UNIQUE (account_id, name)
        );
        CREATE TABLE IF NOT EXISTS senders (
          id                  INTEGER PRIMARY KEY,
          email_norm          TEXT NOT NULL UNIQUE,
          domain              TEXT NOT NULL,
          reg_domain          TEXT NOT NULL,
          reg_domain_skeleton TEXT NOT NULL,
          is_freemail         INTEGER NOT NULL DEFAULT 0,
          first_seen_at       INTEGER,
          last_seen_at        INTEGER
        );
        CREATE TABLE IF NOT EXISTS threads (
          id              INTEGER PRIMARY KEY,
          root_message_id TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS messages (
          id                  INTEGER PRIMARY KEY,
          folder_id           INTEGER NOT NULL REFERENCES folders(id),
          uid                 INTEGER NOT NULL,
          message_id          TEXT,
          direction           TEXT NOT NULL CHECK (direction IN ('in','out')),
          sender_id           INTEGER REFERENCES senders(id),
          from_email_norm     TEXT NOT NULL,
          from_display_name   TEXT,
          reply_to_email_norm TEXT,
          to_me_directly      INTEGER NOT NULL DEFAULT 0,
          n_recipients        INTEGER,
          sent_at             INTEGER NOT NULL,
          sent_hour_local     INTEGER,
          sent_dow_local      INTEGER,
          size_bytes          INTEGER NOT NULL,
          n_attachments       INTEGER NOT NULL DEFAULT 0,
          n_links             INTEGER NOT NULL DEFAULT 0,
          links_extracted     INTEGER NOT NULL DEFAULT 1,
          is_reply            INTEGER NOT NULL DEFAULT 0,
          thread_id           INTEGER REFERENCES threads(id),
          thread_depth        INTEGER NOT NULL DEFAULT 0,
          auth_spf            TEXT,
          auth_dkim           TEXT,
          auth_dmarc          TEXT,
          UNIQUE (folder_id, uid)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, sent_at);
        CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
        CREATE INDEX IF NOT EXISTS idx_messages_msgid  ON messages(message_id);
        CREATE TABLE IF NOT EXISTS message_attachments (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          mime_type  TEXT NOT NULL,
          extension  TEXT,
          size_bytes INTEGER
        );
        CREATE TABLE IF NOT EXISTS message_link_domains (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          reg_domain TEXT NOT NULL,
          n          INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (message_id, reg_domain)
        );
        CREATE TABLE IF NOT EXISTS message_recipients (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          sender_id  INTEGER NOT NULL REFERENCES senders(id),
          PRIMARY KEY (message_id, sender_id)
        );
        CREATE TABLE IF NOT EXISTS sender_profiles (
          sender_id           INTEGER PRIMARY KEY REFERENCES senders(id),
          n_messages          INTEGER NOT NULL DEFAULT 0,
          n_threads           INTEGER NOT NULL DEFAULT 0,
          n_replied_threads   INTEGER NOT NULL DEFAULT 0,
          n_user_initiated    INTEGER NOT NULL DEFAULT 0,
          trust_tier          INTEGER NOT NULL DEFAULT 0,
          hour_histogram      BLOB NOT NULL,
          dow_histogram       BLOB NOT NULL,
          log_size_mean       REAL,
          log_size_m2         REAL,
          links_mean          REAL,
          links_m2            REAL,
          n_with_attachments  INTEGER NOT NULL DEFAULT 0,
          n_with_links        INTEGER NOT NULL DEFAULT 0,
          n_replyto_divergent INTEGER NOT NULL DEFAULT 0,
          first_msg_at        INTEGER,
          last_msg_at         INTEGER,
          max_thread_depth    INTEGER NOT NULL DEFAULT 0,
          updated_at          INTEGER NOT NULL,
          profile_version     INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sender_display_names (
          sender_id     INTEGER NOT NULL REFERENCES senders(id),
          name_norm     TEXT NOT NULL,
          name_skeleton TEXT NOT NULL,
          n             INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (sender_id, name_norm)
        );
        CREATE TABLE IF NOT EXISTS sender_attachment_types (
          sender_id     INTEGER NOT NULL REFERENCES senders(id),
          extension     TEXT NOT NULL,
          mime_type     TEXT NOT NULL,
          n             INTEGER NOT NULL DEFAULT 1,
          first_seen_at INTEGER NOT NULL,
          PRIMARY KEY (sender_id, extension, mime_type)
        );
        CREATE TABLE IF NOT EXISTS sender_link_domains (
          sender_id     INTEGER NOT NULL REFERENCES senders(id),
          reg_domain    TEXT NOT NULL,
          n             INTEGER NOT NULL DEFAULT 1,
          first_seen_at INTEGER NOT NULL,
          PRIMARY KEY (sender_id, reg_domain)
        );
        CREATE TABLE IF NOT EXISTS sender_replyto_addrs (
          sender_id  INTEGER NOT NULL REFERENCES senders(id),
          email_norm TEXT NOT NULL,
          n          INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (sender_id, email_norm)
        );
        CREATE TABLE IF NOT EXISTS thread_reply_credits (
          thread_id INTEGER NOT NULL REFERENCES threads(id),
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          PRIMARY KEY (thread_id, sender_id)
        );
        CREATE TABLE IF NOT EXISTS domain_profiles (
          reg_domain        TEXT PRIMARY KEY,
          n_senders         INTEGER NOT NULL DEFAULT 0,
          n_messages        INTEGER NOT NULL DEFAULT 0,
          n_replied_threads INTEGER NOT NULL DEFAULT 0,
          is_freemail       INTEGER NOT NULL DEFAULT 0,
          first_seen_at     INTEGER,
          last_seen_at      INTEGER
        );
        CREATE TABLE IF NOT EXISTS message_scores (
          message_id            INTEGER PRIMARY KEY
                                REFERENCES messages(id) ON DELETE CASCADE,
          engine_version        TEXT NOT NULL,
          trust_tier_at_scoring INTEGER NOT NULL,
          baseline_n            INTEGER NOT NULL,
          anomaly_score         REAL NOT NULL,
          scored_at             INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_score_features (
          message_id   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          feature      TEXT NOT NULL,
          raw_value    REAL NOT NULL,
          weight       REAL NOT NULL,
          contribution REAL NOT NULL,
          explanation  TEXT NOT NULL,
          PRIMARY KEY (message_id, feature)
        );
        CREATE TABLE IF NOT EXISTS skipped_messages (
          id         INTEGER PRIMARY KEY,
          folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
          uid        INTEGER NOT NULL,
          path       TEXT,
          reason     TEXT NOT NULL,
          skipped_at INTEGER NOT NULL,
          UNIQUE (folder_id, uid)
        );
        """
    )
    conn2.close()


# ---------------------------------------------------------------------------
# framework basics
# ---------------------------------------------------------------------------


def test_registry_has_all_migrations():
    assert sorted(_REGISTRY.keys()) == list(range(1, LATEST_VERSION + 1))


def test_pending_migrations_filters_correctly():
    pending = get_pending_migrations(0)
    versions = [v for v, _, _ in pending]
    assert versions == [1, 2, 3, 4, 5]

    pending = get_pending_migrations(2)
    versions = [v for v, _, _ in pending]
    assert versions == [3, 4, 5]

    pending = get_pending_migrations(LATEST_VERSION)
    assert pending == []


def test_pending_migrations_with_target():
    pending = get_pending_migrations(0, target_version=2)
    versions = [v for v, _, _ in pending]
    assert versions == [1, 2]


# ---------------------------------------------------------------------------
# runner behaviour
# ---------------------------------------------------------------------------


def test_run_migrations_applies_in_order():
    conn = _bare_conn()
    applied = run_migrations(conn)
    assert applied == [1, 2, 3, 4, 5]
    assert get_current_version(conn) == LATEST_VERSION
    conn.close()


def test_run_migrations_idempotent():
    conn = _bare_conn()
    run_migrations(conn)
    applied = run_migrations(conn)
    assert applied == []
    assert get_current_version(conn) == LATEST_VERSION
    conn.close()


def test_failed_migration_rolls_back():
    conn = _bare_conn()
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")

    def bad_apply(c):
        c.execute("CREATE TABLE _mig_fail_marker (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated failure")

    _REGISTRY[99] = ("bad_migration", bad_apply)
    try:
        with pytest.raises(RuntimeError, match="migration 99 .* failed"):
            run_migrations(conn, target_version=99)
        # Marker table must not exist (rolled back).
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "_mig_fail_marker" not in tables
        # Migrations 2 and 3 succeeded before 99 failed.
        assert get_current_version(conn) == LATEST_VERSION
    finally:
        del _REGISTRY[99]
    conn.close()


def test_fk_pragma_restored_after_failure():
    conn = _bare_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")

    def bad_apply(c):
        raise RuntimeError("boom")

    _REGISTRY[99] = ("bad_fk_test", bad_apply)
    try:
        with pytest.raises(RuntimeError):
            run_migrations(conn, target_version=99)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, "foreign_keys pragma must be restored to ON"
    finally:
        del _REGISTRY[99]
    conn.close()


# ---------------------------------------------------------------------------
# end-to-end via connect()
# ---------------------------------------------------------------------------


def test_old_db_gets_migrated(tmp_path):
    path = tmp_path / "old.db"
    _create_v1_db(path)

    conn = storage_db.connect(path)

    # auth_method now accepts 'mail_store'.
    conn.execute(
        "INSERT INTO accounts"
        " (id, email, imap_host, auth_method, created_at)"
        " VALUES (1, 'test@example.com', 'imap.test', 'mail_store', 0)"
    )

    # last_error column exists.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    assert "last_error" in cols

    # sent_at index exists (migration 003).
    idxs = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_messages_sent_at" in idxs

    # schema_version is current.
    assert storage_db.get_meta(conn, "schema_version") == str(LATEST_VERSION)
    conn.close()


def test_fresh_db_at_latest_version():
    conn = storage_db.connect(":memory:")
    assert storage_db.get_meta(conn, "schema_version") == str(LATEST_VERSION)
    conn.close()


def test_duplicate_version_registration():
    with pytest.raises(ValueError, match="already registered"):
        register_migration(version=1, name="duplicate")(lambda c: None)
