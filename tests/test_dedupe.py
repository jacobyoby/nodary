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
