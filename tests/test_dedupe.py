"""Duplicate suppression: a message is ingested once, however many copies arrive."""

from email.message import EmailMessage
from email.utils import format_datetime

from conftest import ME, T0, make_email

from nodary.feature_extraction.extract import record_from_message
from nodary.pipeline import ingest_message


def test_repeated_delivery_is_ingested_once(mailbox):
    msg = make_email("vendor@example.com", message_id="<dup-1@example.com>")
    first = mailbox.deliver(msg)
    second = mailbox.deliver(msg)

    assert second == first
    assert mailbox.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    # the sender's baseline must not double-count the skipped copy
    n = mailbox.conn.execute(
        "SELECT p.n_messages FROM sender_profiles p"
        " JOIN senders s ON s.id = p.sender_id"
        " WHERE s.email_norm = 'vendor@example.com'"
    ).fetchone()[0]
    assert n == 1


def test_forwarded_copy_in_another_folder_is_ingested_once(mailbox):
    msg = make_email("vendor@example.com", message_id="<dup-2@example.com>")
    first = mailbox.deliver(msg)

    rec = record_from_message(msg, direction="in", my_addrs=frozenset({ME}))
    second = ingest_message(mailbox.conn, mailbox.SENT, 500, rec)

    assert second == first
    assert mailbox.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_reused_message_id_with_different_content_is_not_deduped(mailbox):
    # mailing lists reuse Message-IDs; size separates distinct posts
    first = make_email(
        "list@example.com", message_id="<reuse@list.example>", body="digest one"
    )
    second = make_email(
        "list@example.com",
        message_id="<reuse@list.example>",
        body="digest two, with more words than the first one",
    )
    mailbox.deliver(first)
    mailbox.deliver(second)
    assert mailbox.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_missing_message_id_is_never_treated_as_duplicate(mailbox):
    def bare() -> EmailMessage:
        m = EmailMessage()
        m["From"] = "vendor@example.com"
        m["To"] = ME
        m["Date"] = format_datetime(T0)
        m.set_content("no message-id header")
        return m

    mailbox.deliver(bare())
    mailbox.deliver(bare())
    assert mailbox.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_dedupe_partial_unique_index_exists(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_messages_dedupe'"
    ).fetchone()
    assert row is not None
    assert "WHERE message_id IS NOT NULL" in row["sql"]


def test_raw_duplicate_insert_uses_index(conn, mailbox):
    import sqlite3

    from conftest import make_email

    from nodary.feature_extraction.extract import record_from_message
    from nodary.pipeline import ingest_message

    msg = make_email("vendor2@example.com", message_id="<dup-index@example.com>")
    rec = record_from_message(
        msg, direction="in", my_addrs=frozenset({"jacob@myco.com"})
    )
    # first ingest via pipeline creates the constraint row
    ingest_message(conn, 1, 999, rec)
    # raw duplicate INSERT with same triple must violate unique index
    try:
        conn.execute(
            "INSERT INTO messages (folder_id, uid, message_id, direction, sender_id, from_email_norm, sent_at, size_bytes) VALUES (1, 1000, ?, ?, 1, ?, ?, ?)",
            (rec.message_id, "in", rec.from_email_norm, rec.sent_at, rec.size_bytes),
        )
        conn.commit()
        raise AssertionError("expected IntegrityError")
    except sqlite3.IntegrityError as e:
        assert "idx_messages_dedupe" in str(e) or "UNIQUE constraint" in str(e)


def test_pipeline_handles_integrity_error_race(conn, mailbox):
    from conftest import make_email

    from nodary.feature_extraction.extract import record_from_message
    from nodary.pipeline import ingest_message

    msg = make_email("vendor3@example.com", message_id="<race-dedupe@example.com>")
    rec = record_from_message(
        msg, direction="in", my_addrs=frozenset({"jacob@myco.com"})
    )
    first = ingest_message(conn, 1, 800, rec)
    # second call with different folder/uid but same triple should return same id via pipeline guard (and index fallback)
    second = ingest_message(conn, 2, 801, rec)
    assert second == first
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id='<race-dedupe@example.com>'"
        ).fetchone()[0]
        == 1
    )
