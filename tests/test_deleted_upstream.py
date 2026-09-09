"""Tests for server-deleted UID reconciliation (issue #31).

Nodary never purges message rows — instead, reconciliation marks locally
retained facts that no longer exist on the server.  The mark is informational
only and does not exclude facts from scoring baselines.
"""

from __future__ import annotations

from conftest import make_email

from nodary.imap_sync.sync import reconcile_deleted_uids
from nodary.pipeline import rebuild
from nodary.ui import create_app

# ── reconciliation logic ──────────────────────────────────────────────


def test_reconcile_marks_vanished_uid(conn, mailbox):
    """UIDs present locally but absent from the server set get marked."""
    for i in range(3):
        mailbox.deliver(make_email(f"sender{i}@example.com"))
    # UIDs 1, 2, 3 are in the DB.  Server reports only {1, 3}.
    n = reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 3})
    assert n == 1
    # UID 2 is marked deleted_upstream but the row still exists.
    row = conn.execute(
        "SELECT deleted_upstream FROM messages WHERE folder_id=1 AND uid=2"
    ).fetchone()
    assert row is not None
    assert row["deleted_upstream"] == 1
    # UIDs 1 and 3 are not marked.
    for uid in (1, 3):
        row = conn.execute(
            "SELECT deleted_upstream FROM messages WHERE folder_id=1 AND uid=?",
            (uid,),
        ).fetchone()
        assert row["deleted_upstream"] == 0


def test_reconcile_clears_mark_on_reappearance(conn, mailbox):
    """A previously marked UID is un-marked when it reappears on the server."""
    for i in range(3):
        mailbox.deliver(make_email(f"sender{i}@example.com"))
    reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 3})
    # Confirm UID 2 was marked.
    assert (
        conn.execute(
            "SELECT deleted_upstream FROM messages WHERE folder_id=1 AND uid=2"
        ).fetchone()["deleted_upstream"]
        == 1
    )
    # Now UID 2 reappears on the server.
    reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 2, 3})
    assert (
        conn.execute(
            "SELECT deleted_upstream FROM messages WHERE folder_id=1 AND uid=2"
        ).fetchone()["deleted_upstream"]
        == 0
    )


def test_reconcile_no_op_when_sets_match(conn, mailbox):
    """No marks when local and server sets are identical."""
    for i in range(3):
        mailbox.deliver(make_email(f"sender{i}@example.com"))
    n = reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 2, 3})
    assert n == 0
    all_marks = conn.execute(
        "SELECT SUM(deleted_upstream) FROM messages WHERE folder_id=1"
    ).fetchone()[0]
    assert all_marks == 0


def test_reconcile_does_not_delete_rows(conn, mailbox):
    """Reconciliation must never remove message rows."""
    for i in range(3):
        mailbox.deliver(make_email(f"sender{i}@example.com"))
    n_before = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE folder_id=1"
    ).fetchone()[0]
    reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids=set())
    n_after = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE folder_id=1"
    ).fetchone()[0]
    assert n_before == n_after == 3


# ── rebuild determinism with server-deleted messages ──────────────────


def test_rebuild_includes_server_deleted_messages(conn, mailbox):
    """Server-deleted messages still contribute to baselines during rebuild."""
    msg_ids = []
    for i in range(3):
        mid = mailbox.deliver(make_email(f"sender{i}@example.com"))
        msg_ids.append(mid)
    # Get scores before marking.
    scores_before = {
        mid: conn.execute(
            "SELECT anomaly_score FROM message_scores WHERE message_id=?", (mid,)
        ).fetchone()["anomaly_score"]
        for mid in msg_ids
    }
    # Mark UID 2 as server-deleted.
    reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 3})
    conn.commit()
    # Rebuild: all messages (including server-deleted) must be replayed.
    n = rebuild(conn)
    assert n == 3  # all three messages replayed
    # Scores must be identical (deterministic rebuild).
    for mid in msg_ids:
        score_after = conn.execute(
            "SELECT anomaly_score FROM message_scores WHERE message_id=?", (mid,)
        ).fetchone()["anomaly_score"]
        assert score_after == scores_before[mid]


# ── CLI status ────────────────────────────────────────────────────────


def test_status_reports_server_deleted(conn, mailbox, capsys, monkeypatch, tmp_path):
    """nodary status shows server-deleted counts."""
    from nodary.cli import main

    monkeypatch.setenv("NODARY_DB", str(tmp_path / "nodary.db"))
    monkeypatch.setenv("NODARY_DB_KEY", "ab" * 32)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekrit")
    monkeypatch.setattr("nodary.cli.set_account_secret", lambda *_: None)
    main(["add-account", "jacob@example.com", "--host", "imap.example.com"])
    # Directly insert messages and mark one as deleted.
    from nodary.cli import _open

    c = _open()
    c.execute(
        "INSERT INTO folders (id, account_id, name, role) VALUES (1, 1, 'INBOX', 'inbox')"
    )
    c.execute(
        "INSERT INTO senders (email_norm, domain, reg_domain, reg_domain_skeleton,"
        " is_freemail) VALUES ('a@x.com', 'x.com', 'x.com', 'x.com', 0)"
    )
    for uid in (1, 2, 3):
        c.execute(
            "INSERT INTO messages (folder_id, uid, message_id, direction, sender_id,"
            " from_email_norm, sent_at, size_bytes, deleted_upstream)"
            " VALUES (1, ?, '<test@x>', 'in', 1, 'a@x.com', 1700000000, 100, ?)",
            (uid, 1 if uid == 2 else 0),
        )
    c.commit()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "server-deleted" in out
    assert "INBOX: 1" in out


# ── API status ────────────────────────────────────────────────────────


def test_api_status_includes_server_deleted_fields(conn, mailbox):
    """/api/status includes server_deleted_count and server_deleted_by_folder."""
    for i in range(3):
        mailbox.deliver(make_email(f"sender{i}@example.com"))
    reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 3})
    conn.commit()

    app = create_app(conn)
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/status").get_json()
    assert "server_deleted_count" in r
    assert "server_deleted_by_folder" in r
    assert r["server_deleted_count"] == 1
    assert len(r["server_deleted_by_folder"]) == 1
    assert r["server_deleted_by_folder"][0]["folder"] == "INBOX"
    assert r["server_deleted_by_folder"][0]["count"] == 1


def test_api_status_server_deleted_zero_by_default(conn, mailbox):
    """/api/status reports zero server-deleted when none are marked."""
    mailbox.deliver(make_email("a@example.com"))
    app = create_app(conn)
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/status").get_json()
    assert r["server_deleted_count"] == 0
    assert r["server_deleted_by_folder"] == []


def test_api_status_server_deleted_respects_account_filter(conn):
    """server_deleted fields respect the account filter."""
    # Add a second account with a server-deleted message.
    conn.execute(
        "INSERT INTO accounts (id, email, imap_host, auth_method, created_at)"
        " VALUES (2, 'alice@other.com', 'imap.other', 'app_password', 0)"
    )
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (2, 'alice@other.com')"
    )
    conn.execute(
        "INSERT INTO folders (id, account_id, name, role) VALUES (3, 2, 'INBOX', 'inbox')"
    )
    conn.execute(
        "INSERT INTO senders (email_norm, domain, reg_domain, reg_domain_skeleton,"
        " is_freemail) VALUES ('b@y.com', 'y.com', 'y.com', 'y.com', 0)"
    )
    conn.execute(
        "INSERT INTO messages (folder_id, uid, message_id, direction, sender_id,"
        " from_email_norm, sent_at, size_bytes, deleted_upstream)"
        " VALUES (3, 1, '<test@y>', 'in', (SELECT id FROM senders WHERE email_norm='b@y.com'),"
        " 'b@y.com', 1700000000, 100, 1)"
    )
    # Also add a server-deleted message to account 1.
    conn.execute(
        "INSERT INTO senders (email_norm, domain, reg_domain, reg_domain_skeleton,"
        " is_freemail) VALUES ('c@z.com', 'z.com', 'z.com', 'z.com', 0)"
        " ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO messages (folder_id, uid, message_id, direction, sender_id,"
        " from_email_norm, sent_at, size_bytes, deleted_upstream)"
        " VALUES (1, 99, '<test@z>', 'in', (SELECT id FROM senders WHERE email_norm='c@z.com'),"
        " 'c@z.com', 1700000000, 100, 1)"
    )
    conn.commit()

    app = create_app(conn)
    app.config["TESTING"] = True
    client = app.test_client()

    # All accounts.
    r = client.get("/api/status?account=all").get_json()
    assert r["server_deleted_count"] == 2

    # Account 1 only.
    r = client.get("/api/status?account=1").get_json()
    assert r["server_deleted_count"] == 1

    # Account 2 only.
    r = client.get("/api/status?account=2").get_json()
    assert r["server_deleted_count"] == 1


# ── migration ─────────────────────────────────────────────────────────


def test_migration_005_adds_deleted_upstream_column(tmp_path):
    """Migration 005 adds the deleted_upstream column to an old messages table."""
    import sqlite3

    from nodary.storage import db as storage_db
    from nodary.storage.migrations import LATEST_VERSION

    # Create a DB with the old messages table (no deleted_upstream column).
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '4');
        CREATE TABLE accounts (
          id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
          imap_host TEXT NOT NULL, imap_port INTEGER NOT NULL DEFAULT 993,
          auth_method TEXT NOT NULL
            CHECK (auth_method IN ('oauth2','app_password','mail_store')),
          created_at INTEGER NOT NULL
        );
        CREATE TABLE user_identities (
          account_id INTEGER NOT NULL REFERENCES accounts(id),
          email_norm TEXT NOT NULL,
          PRIMARY KEY (account_id, email_norm)
        );
        CREATE TABLE folders (
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
          name TEXT NOT NULL, role TEXT NOT NULL
            CHECK (role IN ('inbox','sent','archive','other')),
          uidvalidity INTEGER, last_seen_uid INTEGER NOT NULL DEFAULT 0,
          last_synced_at INTEGER, UNIQUE (account_id, name)
        );
        CREATE TABLE senders (
          id INTEGER PRIMARY KEY, email_norm TEXT NOT NULL UNIQUE,
          domain TEXT NOT NULL, reg_domain TEXT NOT NULL,
          reg_domain_skeleton TEXT NOT NULL, is_freemail INTEGER NOT NULL DEFAULT 0,
          first_seen_at INTEGER, last_seen_at INTEGER
        );
        CREATE TABLE threads (id INTEGER PRIMARY KEY, root_message_id TEXT UNIQUE);
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, folder_id INTEGER NOT NULL REFERENCES folders(id),
          uid INTEGER NOT NULL, message_id TEXT,
          direction TEXT NOT NULL CHECK (direction IN ('in','out')),
          sender_id INTEGER REFERENCES senders(id),
          from_email_norm TEXT NOT NULL, from_display_name TEXT,
          reply_to_email_norm TEXT, to_me_directly INTEGER NOT NULL DEFAULT 0,
          n_recipients INTEGER, sent_at INTEGER NOT NULL,
          sent_hour_local INTEGER, sent_dow_local INTEGER,
          size_bytes INTEGER NOT NULL, n_attachments INTEGER NOT NULL DEFAULT 0,
          n_links INTEGER NOT NULL DEFAULT 0, links_extracted INTEGER NOT NULL DEFAULT 1,
          is_reply INTEGER NOT NULL DEFAULT 0,
          thread_id INTEGER REFERENCES threads(id),
          thread_depth INTEGER NOT NULL DEFAULT 0,
          auth_spf TEXT, auth_dkim TEXT, auth_dmarc TEXT,
          UNIQUE (folder_id, uid)
        );
        CREATE TABLE message_attachments (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          mime_type TEXT NOT NULL, extension TEXT, size_bytes INTEGER
        );
        CREATE TABLE message_link_domains (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          reg_domain TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (message_id, reg_domain)
        );
        CREATE TABLE message_recipients (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          PRIMARY KEY (message_id, sender_id)
        );
        CREATE TABLE sender_profiles (
          sender_id INTEGER PRIMARY KEY REFERENCES senders(id),
          n_messages INTEGER NOT NULL DEFAULT 0, n_threads INTEGER NOT NULL DEFAULT 0,
          n_replied_threads INTEGER NOT NULL DEFAULT 0,
          n_user_initiated INTEGER NOT NULL DEFAULT 0,
          trust_tier INTEGER NOT NULL DEFAULT 0,
          hour_histogram BLOB NOT NULL, dow_histogram BLOB NOT NULL,
          log_size_mean REAL, log_size_m2 REAL, links_mean REAL, links_m2 REAL,
          n_with_attachments INTEGER NOT NULL DEFAULT 0,
          n_with_links INTEGER NOT NULL DEFAULT 0,
          n_replyto_divergent INTEGER NOT NULL DEFAULT 0,
          first_msg_at INTEGER, last_msg_at INTEGER,
          max_thread_depth INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL, profile_version INTEGER NOT NULL
        );
        CREATE TABLE sender_display_names (
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          name_norm TEXT NOT NULL, name_skeleton TEXT NOT NULL,
          n INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (sender_id, name_norm)
        );
        CREATE TABLE sender_attachment_types (
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          extension TEXT NOT NULL, mime_type TEXT NOT NULL,
          n INTEGER NOT NULL DEFAULT 1, first_seen_at INTEGER NOT NULL,
          PRIMARY KEY (sender_id, extension, mime_type)
        );
        CREATE TABLE sender_link_domains (
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          reg_domain TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 1,
          first_seen_at INTEGER NOT NULL, PRIMARY KEY (sender_id, reg_domain)
        );
        CREATE TABLE sender_replyto_addrs (
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          email_norm TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (sender_id, email_norm)
        );
        CREATE TABLE thread_reply_credits (
          thread_id INTEGER NOT NULL REFERENCES threads(id),
          sender_id INTEGER NOT NULL REFERENCES senders(id),
          PRIMARY KEY (thread_id, sender_id)
        );
        CREATE TABLE domain_profiles (
          reg_domain TEXT PRIMARY KEY, n_senders INTEGER NOT NULL DEFAULT 0,
          n_messages INTEGER NOT NULL DEFAULT 0,
          n_replied_threads INTEGER NOT NULL DEFAULT 0,
          is_freemail INTEGER NOT NULL DEFAULT 0,
          first_seen_at INTEGER, last_seen_at INTEGER
        );
        CREATE TABLE message_scores (
          message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
          engine_version TEXT NOT NULL, trust_tier_at_scoring INTEGER NOT NULL,
          baseline_n INTEGER NOT NULL, anomaly_score REAL NOT NULL,
          scored_at INTEGER NOT NULL
        );
        CREATE TABLE message_score_features (
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          feature TEXT NOT NULL, raw_value REAL NOT NULL, weight REAL NOT NULL,
          contribution REAL NOT NULL, explanation TEXT NOT NULL,
          PRIMARY KEY (message_id, feature)
        );
        CREATE TABLE skipped_messages (
          id INTEGER PRIMARY KEY,
          folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
          uid INTEGER NOT NULL, path TEXT, reason TEXT NOT NULL,
          skipped_at INTEGER NOT NULL,
          UNIQUE (folder_id, uid)
        );
        """
    )
    conn.close()

    # Open with connect() — migrations should run.
    c = storage_db.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
    assert "deleted_upstream" in cols
    assert storage_db.get_meta(c, "schema_version") == str(LATEST_VERSION)
    c.close()


def test_migration_005_idempotent(conn):
    """Running migration 005 twice is harmless."""
    from nodary.storage.migrations import run_migrations

    # The column already exists from schema.sql — re-running should be a no-op.
    applied = run_migrations(conn)
    assert applied == []  # nothing to apply


# ── dashboard renders server-deleted info ─────────────────────────────


def test_dashboard_renders_server_deleted_in_status(conn, mailbox):
    """The dashboard status strip includes server-deleted information."""
    for i in range(3):
        mailbox.deliver(make_email(f"sender{i}@example.com"))
    reconcile_deleted_uids(conn, account_id=1, folder_id=1, server_uids={1, 3})
    conn.commit()

    app = create_app(conn)
    app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert "server_deleted_count" in html or "server-deleted" in html
