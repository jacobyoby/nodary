"""The scoring engine is deterministic and rebuild-stable: replaying all
facts from scratch reproduces every profile, tier, and score exactly."""

from datetime import timedelta

from conftest import T0, make_email

from nodary.pipeline import rebuild


def _populate(mailbox):
    mailbox.establish_contact("dana.ito@acme-corp.com", display="Dana Ito", n=12)
    mailbox.establish_contact("sam@partnerfirm.com", display="Sam Okafor", n=15)
    # lookalike phish
    mailbox.deliver(
        make_email(
            "dana.ito@acme-c0rp.com",
            display="Dana Ito",
            when=T0 + timedelta(days=50),
            attachments=[("invoice.zip", "application/zip", b"PK")],
        )
    )
    # behavior shift
    mailbox.deliver(
        make_email(
            "sam@partnerfirm.com",
            display="Sam Okafor",
            when=T0 + timedelta(days=55),
            body="https://never-seen-before.net/x",
            attachments=[("doc.zip", "application/zip", b"PK")],
            reply_to="sam@elsewhere.net",
        )
    )
    # cold outreach
    mailbox.deliver(
        make_email(
            "bd@growthly.io",
            when=T0 + timedelta(days=56),
            body="demo at https://growthly.io/x",
        )
    )


def _snapshot(conn):
    scores = conn.execute(
        "SELECT message_id, trust_tier_at_scoring, baseline_n, anomaly_score"
        " FROM message_scores ORDER BY message_id"
    ).fetchall()
    features = conn.execute(
        "SELECT message_id, feature, raw_value, weight, contribution, explanation"
        " FROM message_score_features ORDER BY message_id, feature"
    ).fetchall()
    tiers = conn.execute(
        "SELECT sender_id, trust_tier, n_messages, n_replied_threads,"
        " n_user_initiated FROM sender_profiles ORDER BY sender_id"
    ).fetchall()
    return (
        [tuple(r) for r in scores],
        [tuple(r) for r in features],
        [tuple(r) for r in tiers],
    )


def test_rebuild_reproduces_scores_exactly(conn, mailbox):
    _populate(mailbox)
    before = _snapshot(conn)
    assert before[0], "populate produced no scores"

    n = rebuild(conn)
    assert n == conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    after = _snapshot(conn)
    assert before == after


def test_rebuild_twice_is_idempotent(conn, mailbox):
    _populate(mailbox)
    rebuild(conn)
    first = _snapshot(conn)
    rebuild(conn)
    assert _snapshot(conn) == first
