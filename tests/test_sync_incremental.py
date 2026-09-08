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


def test_direction_uses_normalized_addresses(conn):
    """A dotted-gmail identity must match its dotless normalized form (and
    vice versa): direction detection compares normalized, not raw substrings.
    The self-From copies carry a non-failing AR stamp so they may stay
    outgoing at all."""
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (1, ?)",
        ("jacob.e.durham@gmail.com",),
    )
    conn.commit()
    stamped = "mx.example.com; spf=pass; dmarc=pass"
    t = FakeTransport()
    t.add(
        "INBOX",
        make_email("Jacob.E.Durham@gmail.com", display="Jacob", auth_results=stamped),
    )
    t.add(
        "INBOX",
        make_email("jacobedurham@gmail.com", display="Jacob", auth_results=stamped),
    )
    t.add("INBOX", make_email("dana@acme-corp.com"))
    sync_account(conn, t, account_id=1)
    rows = conn.execute(
        "SELECT from_email_norm, direction FROM messages ORDER BY uid"
    ).fetchall()
    dirs = {r["from_email_norm"]: r["direction"] for r in rows}
    assert dirs["jacobedurham@gmail.com"] == "out"
    assert dirs["dana@acme-corp.com"] == "in"


def test_self_from_classification_needs_a_verdict_or_sent_folder(conn):
    """Self-From mail is outgoing only from the Sent folder or when a
    receiving server stamped Authentication-Results without a DMARC failure.
    No verdict at all is the spoofing blind spot and must be scored."""
    t = FakeTransport()
    t.add(
        "INBOX",
        make_email(ME, display="Jacob", message_id="<self-1@test>"),
    )  # no verdict: score it
    t.add(
        "INBOX",
        make_email(
            ME,
            display="Jacob",
            message_id="<self-2@test>",
            auth_results="mx.example.com; spf=fail; dmarc=fail",
        ),
    )  # DMARC fail: score it
    t.add(
        "INBOX",
        make_email(
            ME,
            display="Jacob",
            message_id="<self-3@test>",
            auth_results="mx.example.com; spf=pass; dmarc=pass",
        ),
    )  # stamped and not failing: genuine self-sent copy
    t.add(
        "Sent",
        make_email(
            ME,
            to="dana@acme-corp.com",
            when=T0 + timedelta(hours=1),
            message_id="<self-4@test>",
        ),
    )
    sync_account(conn, t, account_id=1)
    dirs = [
        r["direction"]
        for r in conn.execute(
            "SELECT direction FROM messages ORDER BY folder_id, uid"
        ).fetchall()
    ]
    assert dirs == ["in", "in", "out", "out"]
    scored = conn.execute(
        "SELECT COUNT(*) FROM message_scores ms"
        " JOIN messages m ON m.id = ms.message_id WHERE m.direction = 'in'"
    ).fetchone()[0]
    assert scored == 2


def test_base64_decode_failure_marks_links_not_extracted(conn):
    """A base64 part with broken padding must not be stored as a fully
    scanned empty body — same treatment as an oversized part."""
    t = FakeTransport()
    msg = make_email("sender@vendor.io", when=T0)
    msg.set_payload("abc")  # invalid base64 padding
    msg.replace_header("Content-Transfer-Encoding", "base64")
    t.add("INBOX", msg)
    sync_account(conn, t, account_id=1)
    row = conn.execute("SELECT links_extracted FROM messages").fetchone()
    assert row is not None
    assert row["links_extracted"] == 0


def test_high_water_mark_stops_at_fetch_gap(conn):
    """UIDs the transport could not serve must be retried next sync, not
    skipped forever by an advancing high-water mark."""
    t = FakeTransport()
    t.add("INBOX", make_email("dana@acme-corp.com", when=T0))
    u2 = t.add("INBOX", make_email("erin@acme-corp.com", when=T0))
    t.add("INBOX", make_email("faye@acme-corp.com", when=T0))
    t.hide(u2)  # indexed but unfetchable, like a not-yet-downloaded .emlx
    stats = sync_account(conn, t, account_id=1)
    assert stats.new_messages == 1  # only the message before the gap
    t.unhide(u2)
    stats2 = sync_account(conn, t, account_id=1)
    assert stats2.new_messages == 2  # gap message and its successor
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
