"""Large-mailbox backfill: age-gated text-part fetch, batch-size tuning,
and performance budget for 10k-message first-run sync."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from conftest import T0, make_email
from fake_imap import FakeTransport

from nodary.imap_sync.sync import (
    TEXT_FETCH_MAX_AGE_DAYS,
    sync_account,
)


def _msg_count(conn):
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


# ── Age-gated text-part fetch ───────────────────────────────────────────


def test_old_messages_skip_text_part_fetch(conn):
    """Messages older than TEXT_FETCH_MAX_AGE_DAYS must have
    links_extracted=0 and no text parts fetched."""
    t = FakeTransport()
    now = datetime.now(UTC)
    old_date = now - timedelta(days=TEXT_FETCH_MAX_AGE_DAYS + 10)

    t.add(
        "INBOX",
        make_email(
            "old-sender@example.com",
            when=old_date,
            body="old body https://old-link.example.com/path",
        ),
    )

    # Track fetch_part calls
    original_fetch_part = t.fetch_part
    fetch_count = [0]

    def counting_fetch_part(uid, section):
        fetch_count[0] += 1
        return original_fetch_part(uid, section)

    t.fetch_part = counting_fetch_part

    sync_account(conn, t, account_id=1, text_fetch_max_age_days=TEXT_FETCH_MAX_AGE_DAYS)

    row = conn.execute("SELECT links_extracted FROM messages WHERE uid = 1").fetchone()
    assert row["links_extracted"] == 0
    assert fetch_count[0] == 0  # no text parts fetched


def test_recent_messages_fetch_text_parts_normally(conn):
    """Messages within the age threshold must have text parts fetched."""
    t = FakeTransport()
    now = datetime.now(UTC)
    recent_date = now - timedelta(days=5)

    t.add(
        "INBOX",
        make_email(
            "recent-sender@example.com",
            when=recent_date,
            body="recent body https://recent-link.example.com/path",
            html="<a href='https://tracker.example.net/click'>click</a>",
        ),
    )

    sync_account(conn, t, account_id=1, text_fetch_max_age_days=TEXT_FETCH_MAX_AGE_DAYS)

    row = conn.execute("SELECT links_extracted FROM messages WHERE uid = 1").fetchone()
    assert row["links_extracted"] == 1

    # Verify link domains were actually extracted (reg_domain is the
    # registrable domain, e.g. "example.com" not "recent-link.example.com")
    domains = {
        r["reg_domain"]
        for r in conn.execute("SELECT reg_domain FROM message_link_domains")
    }
    assert len(domains) > 0


def test_age_gate_boundary(conn):
    """Messages exactly at the boundary: within threshold → fetched."""
    t = FakeTransport()
    now = datetime.now(UTC)
    boundary_date = now - timedelta(days=TEXT_FETCH_MAX_AGE_DAYS - 1)

    t.add(
        "INBOX",
        make_email(
            "boundary-sender@example.com",
            when=boundary_date,
            body="boundary body",
        ),
    )

    sync_account(conn, t, account_id=1, text_fetch_max_age_days=TEXT_FETCH_MAX_AGE_DAYS)

    row = conn.execute("SELECT links_extracted FROM messages WHERE uid = 1").fetchone()
    assert row["links_extracted"] == 1


def test_text_fetch_age_days_override(conn):
    """--text-fetch-age-days override: a shorter window skips more messages."""
    t = FakeTransport()
    now = datetime.now(UTC)
    medium_date = now - timedelta(days=30)

    t.add(
        "INBOX",
        make_email(
            "medium-sender@example.com",
            when=medium_date,
            body="medium body https://medium.example.com/path",
        ),
    )

    # With default 90 days, this message (30 days old) would be fetched
    sync_account(conn, t, account_id=1, text_fetch_max_age_days=90)
    row = conn.execute("SELECT links_extracted FROM messages WHERE uid = 1").fetchone()
    assert row["links_extracted"] == 1

    # Clean up for next sync
    conn.execute("DELETE FROM messages")
    conn.execute("UPDATE folders SET last_seen_uid = 0")
    conn.commit()

    # With 10-day window, this 30-day-old message should skip text fetch
    sync_account(conn, t, account_id=1, text_fetch_max_age_days=10)
    row = conn.execute("SELECT links_extracted FROM messages WHERE uid = 1").fetchone()
    assert row["links_extracted"] == 0


def test_text_fetch_age_days_none_fetches_all(conn):
    """text_fetch_max_age_days=None disables the age gate entirely."""
    t = FakeTransport()
    now = datetime.now(UTC)
    very_old = now - timedelta(days=365 * 5)

    t.add(
        "INBOX",
        make_email(
            "ancient-sender@example.com",
            when=very_old,
            body="ancient body https://ancient.example.com/path",
        ),
    )

    sync_account(conn, t, account_id=1, text_fetch_max_age_days=None)

    row = conn.execute("SELECT links_extracted FROM messages WHERE uid = 1").fetchone()
    assert row["links_extracted"] == 1


def test_identity_features_scored_when_text_skipped(conn):
    """Even when text parts are skipped, identity features must still be
    scored. The message must be ingested and scored."""
    t = FakeTransport()
    now = datetime.now(UTC)
    old_date = now - timedelta(days=TEXT_FETCH_MAX_AGE_DAYS + 30)

    # Create enough history for a baseline
    for i in range(5):
        t.add(
            "INBOX",
            make_email(
                "scored-sender@example.com",
                when=old_date + timedelta(days=i),
                body=f"message {i}",
            ),
        )

    sync_account(conn, t, account_id=1, text_fetch_max_age_days=TEXT_FETCH_MAX_AGE_DAYS)

    # All messages ingested
    assert _msg_count(conn) == 5

    # links_extracted=0 for all (they're all old)
    rows = conn.execute("SELECT links_extracted FROM messages").fetchall()
    assert all(r["links_extracted"] == 0 for r in rows)

    # Sender profile exists (identity features were processed)
    profile = conn.execute(
        """SELECT p.trust_tier FROM sender_profiles p
           JOIN senders s ON s.id = p.sender_id
           WHERE s.email_norm = 'scored-sender@example.com'"""
    ).fetchone()
    assert profile is not None


# ── Batch size ──────────────────────────────────────────────────────────


def test_custom_batch_size(conn):
    """--batch-size flag controls the number of UIDs fetched per batch."""
    t = FakeTransport()
    for i in range(10):
        t.add("INBOX", make_email(f"batch{i}@example.com", when=T0))

    # Use a small batch size
    sync_account(conn, t, account_id=1, batch_size=3)
    assert _msg_count(conn) == 10


def test_batch_size_one(conn):
    """batch_size=1 should still work correctly."""
    t = FakeTransport()
    for i in range(5):
        t.add("INBOX", make_email(f"single{i}@example.com", when=T0))

    sync_account(conn, t, account_id=1, batch_size=1)
    assert _msg_count(conn) == 5


# ── Performance budget ──────────────────────────────────────────────────


def test_10k_backfill_within_time_budget(conn):
    """10k-message backfill (CI-scaled) must complete within a reasonable
    time budget. This is a smoke test for performance regressions, not a
    precise benchmark."""
    t = FakeTransport()
    now = datetime.now(UTC)

    # 10k messages spread over 2 years with varied senders
    senders = [f"perf{i}@domain{i % 20}.example" for i in range(50)]
    for uid in range(10_000):
        days_ago = int((uid / 10_000) * 730)
        when = now - timedelta(days=days_ago, hours=uid % 24)
        sender = senders[uid % len(senders)]
        t.add(
            "INBOX",
            make_email(sender, when=when, body=f"perf message {uid}"),
        )

    start = time.monotonic()
    stats = sync_account(conn, t, account_id=1, text_fetch_max_age_days=90)
    elapsed = time.monotonic() - start

    assert stats.new_messages == 10_000
    # Budget: 120 seconds for CI (generous to avoid flaky failures).
    # Local machines should complete in ~10-30 seconds.
    assert elapsed < 120, f"10k backfill took {elapsed:.1f}s, budget is 120s"
