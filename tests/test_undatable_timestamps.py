"""nodary#7 (F10): undatable and naive Date headers must not corrupt time math.

- A missing/unparsable Date stores sent_at=0 as the unknown mark, but that
  mark must not seed first/last-seen timelines or fake a Tier-2 span.
- A Date without a UTC offset is read as UTC, independent of the machine's
  local timezone.
"""

import os
import time
from datetime import UTC, datetime, timedelta

from conftest import ME, T0, make_email

from nodary.feature_extraction.extract import record_from_message


def _undated(peer, message_id):
    msg = make_email(peer, when=T0, message_id=message_id)
    del msg["Date"]
    return msg


def _profile_times(mailbox, peer):
    return mailbox.conn.execute(
        "SELECT first_msg_at, last_msg_at FROM sender_profiles"
        " JOIN senders ON senders.id = sender_profiles.sender_id"
        " WHERE email_norm = ?",
        (peer,),
    ).fetchone()


def test_missing_date_marks_sent_at_zero_but_leaves_timeline_empty(mailbox):
    peer = "ghost@acme-corp.com"
    row_id = mailbox.deliver(_undated(peer, "<undated-1@test>"))
    stored = mailbox.conn.execute(
        "SELECT sent_at FROM messages WHERE id = ?", (row_id,)
    ).fetchone()["sent_at"]
    assert stored == 0
    prof = _profile_times(mailbox, peer)
    assert prof["first_msg_at"] is None
    assert prof["last_msg_at"] is None
    assert mailbox.tier_of(peer) == 0


def test_unparsable_date_behaves_like_missing_date(mailbox):
    peer = "junk@acme-corp.com"
    msg = make_email(peer, when=T0, message_id="<junk@test>")
    msg.replace_header("Date", "not a date")
    mailbox.deliver(msg)
    prof = _profile_times(mailbox, peer)
    assert prof["first_msg_at"] is None
    assert prof["last_msg_at"] is None


def test_undated_message_does_not_fake_a_tier2_span(mailbox):
    peer = "ghost@acme-corp.com"
    mailbox.deliver(make_email(peer, when=T0, message_id="<dated-1@test>"))
    mailbox.deliver(_undated(peer, "<undated-2@test>"))
    # Two days apart is far short of the 7-day Tier-2 span; before the fix
    # the 0-epoch first_msg_at scored this message as tier 2.
    row_id = mailbox.deliver(
        make_email(peer, when=T0 + timedelta(days=2), message_id="<dated-3@test>")
    )
    scored = mailbox.conn.execute(
        "SELECT trust_tier_at_scoring FROM message_scores WHERE message_id = ?",
        (row_id,),
    ).fetchone()["trust_tier_at_scoring"]
    assert scored == 0
    assert mailbox.tier_of(peer) == 0
    prof = _profile_times(mailbox, peer)
    assert prof["first_msg_at"] == int(T0.timestamp())
    assert prof["last_msg_at"] == int((T0 + timedelta(days=2)).timestamp())


def test_naive_date_read_as_utc_regardless_of_machine_timezone():
    prev_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, no DST
    time.tzset()
    try:
        msg = make_email("naive@acme-corp.com", when=T0, message_id="<naive@test>")
        msg.replace_header("Date", "Mon, 01 Jan 2024 12:00:00")
        rec = record_from_message(msg, direction="in", my_addrs=frozenset({ME}))
    finally:
        if prev_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = prev_tz
        time.tzset()
    assert rec.sent_at == int(datetime(2024, 1, 1, 12, 0, tzinfo=UTC).timestamp())
    assert rec.sent_hour_local == 12
