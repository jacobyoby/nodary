"""Incremental sync: high-water marks, UIDVALIDITY invalidation, and the
header+structure fetch path (link/attachment extraction without full bodies)."""

from datetime import timedelta

from conftest import ME, T0, make_email
from fake_imap import FakeTransport

from nodary.imap_sync.sync import sync_account


def _msg_count(conn):
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_second_sync_fetches_nothing(conn):
    t = FakeTransport()
    for i in range(5):
        t.add("INBOX", make_email("dana@acme-corp.com", when=T0 + timedelta(days=i)))
    stats = sync_account(conn, t, account_id=1)
    assert stats.new_messages == 5
    assert _msg_count(conn) == 5

    fetched_before = t.meta_fetches
    stats2 = sync_account(conn, t, account_id=1)
    assert stats2.new_messages == 0
    assert t.meta_fetches == fetched_before  # zero re-downloads


def test_only_new_uids_fetched_after_new_mail(conn):
    t = FakeTransport()
    t.add("INBOX", make_email("dana@acme-corp.com", when=T0))
    sync_account(conn, t, account_id=1)

    t.add("INBOX", make_email("dana@acme-corp.com", when=T0 + timedelta(days=1)))
    before = t.meta_fetches
    stats = sync_account(conn, t, account_id=1)
    assert stats.new_messages == 1
    assert t.meta_fetches == before + 1


def test_uidvalidity_change_invalidates_and_refetches(conn):
    t = FakeTransport()
    for i in range(3):
        t.add("INBOX", make_email("dana@acme-corp.com", when=T0 + timedelta(days=i)))
    sync_account(conn, t, account_id=1)
    assert _msg_count(conn) == 3

    t.bump_uidvalidity("INBOX")
    stats = sync_account(conn, t, account_id=1)
    assert "INBOX" in stats.invalidated_folders
    assert _msg_count(conn) == 3  # wiped and refetched, not duplicated


def test_structure_extracted_without_body_storage(conn):
    t = FakeTransport()
    t.add(
        "INBOX",
        make_email(
            "sender@vendor.io",
            when=T0,
            body="download: https://files.vendor.io/report plus text",
            html="<a href='https://tracker.clicks-r-us.net/c?id=9'>click</a>",
            attachments=[("report.pdf", "application/pdf", b"%PDF-1.4 fake")],
        ),
    )
    sync_account(conn, t, account_id=1)

    att = conn.execute("SELECT * FROM message_attachments").fetchone()
    assert att["mime_type"] == "application/pdf"
    assert att["extension"] == "pdf"

    domains = {
        r["reg_domain"]
        for r in conn.execute("SELECT reg_domain FROM message_link_domains")
    }
    assert domains == {"vendor.io", "clicks-r-us.net"}

    # privacy: no body text, subject, or filename anywhere in the database
    msg_row = dict(conn.execute("SELECT * FROM messages").fetchone())
    blob = " ".join(str(v) for v in msg_row.values())
    assert "download:" not in blob and "synthetic" not in blob
    assert "report.pdf" not in blob


def test_sent_folder_gives_two_way_tier(conn):
    t = FakeTransport()
    incoming = make_email("dana@acme-corp.com", when=T0)
    t.add("INBOX", incoming)
    t.add(
        "Sent",
        make_email(
            ME,
            to="dana@acme-corp.com",
            when=T0 + timedelta(hours=2),
            in_reply_to=incoming["Message-ID"],
            references=[incoming["Message-ID"]],
        ),
    )
    sync_account(conn, t, account_id=1)

    tier = conn.execute(
        """SELECT p.trust_tier FROM sender_profiles p
           JOIN senders s ON s.id = p.sender_id
           WHERE s.email_norm = 'dana@acme-corp.com'"""
    ).fetchone()["trust_tier"]
    assert tier == 3
