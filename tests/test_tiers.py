from datetime import timedelta

from conftest import ME, T0, make_email


def test_tier_progression(mailbox):
    peer = "dana@acme-corp.com"

    # one message: never enough for tier 2
    first = make_email(peer, when=T0)
    mailbox.deliver(first)
    assert mailbox.tier_of(peer) == 0

    # second message within a week: still tier 0 (span too short)
    mailbox.deliver(make_email(peer, when=T0 + timedelta(days=2)))
    assert mailbox.tier_of(peer) == 0

    # a message 8 days after the first: tier 2 (prior one-way contact)
    mailbox.deliver(make_email(peer, when=T0 + timedelta(days=8)))
    assert mailbox.tier_of(peer) == 2

    # the user replies: tier 3 (two-way)
    mailbox.reply_to(peer, first, when=T0 + timedelta(days=9))
    assert mailbox.tier_of(peer) == 3


def test_tier1_domain_propagation_non_freemail(mailbox):
    # establish two-way contact with one person at acme-corp.com
    mailbox.establish_contact("dana@acme-corp.com", n=3)
    assert mailbox.tier_of("dana@acme-corp.com") == 3

    # a NEW sender at the same org domain arrives: tier 1 at scoring time
    row_id = mailbox.deliver(
        make_email("billing@acme-corp.com", when=T0 + timedelta(days=30))
    )
    score_row = mailbox.conn.execute(
        "SELECT trust_tier_at_scoring FROM message_scores WHERE message_id = ?",
        (row_id,),
    ).fetchone()
    assert score_row["trust_tier_at_scoring"] == 1


def test_tier1_never_propagates_for_freemail(mailbox):
    mailbox.establish_contact("friend@gmail.com", n=3)
    assert mailbox.tier_of("friend@gmail.com") == 3

    row_id = mailbox.deliver(
        make_email("stranger@gmail.com", when=T0 + timedelta(days=30))
    )
    score_row = mailbox.conn.execute(
        "SELECT trust_tier_at_scoring FROM message_scores WHERE message_id = ?",
        (row_id,),
    ).fetchone()
    assert score_row["trust_tier_at_scoring"] == 0


def test_user_initiated_contact_is_tier3(mailbox):
    peer = "newvendor@supplies.io"
    mailbox.send(make_email(ME, to=peer, when=T0))
    assert mailbox.tier_of(peer) == 3
