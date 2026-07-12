"""Fixture: compromised-contact behavior shift — an established Tier-3
correspondent suddenly behaves like an account-takeover: new attachment type,
links to never-seen domains, sent at an hour they never write, replies
redirected."""

from datetime import UTC, datetime, timedelta

from conftest import T0, make_email

PEER = "sam.okafor@partnerfirm.com"


def _baseline(mailbox, n=20):
    mailbox.establish_contact(PEER, display="Sam Okafor", n=n)
    assert mailbox.tier_of(PEER) == 3


def test_takeover_style_message_flags_stack(mailbox):
    _baseline(mailbox)

    attack = make_email(
        PEER,
        display="Sam Okafor",
        when=datetime(2026, 3, 20, 3, 12, tzinfo=UTC),  # 03:12 sender-local
        body="urgent - wire details changed, see attached and confirm at "
        "https://secure-docs-verify.net/login",
        attachments=[("payment_details.zip", "application/zip", b"PK\x03\x04x")],
        reply_to="sam.okafor@consultant-mail.net",
    )
    row_id = mailbox.deliver(attack)
    feats = mailbox.features_of(row_id)

    assert "first_attachment_ever" in feats
    assert "in 20 messages" in feats["first_attachment_ever"]["explanation"]
    assert "attachment of any kind" in feats["first_attachment_ever"]["explanation"]

    assert "link_domain_novelty" in feats
    assert "secure-docs-verify.net" in feats["link_domain_novelty"]["explanation"]

    assert "send_hour_anomaly" in feats
    assert "03:00 sender-local" in feats["send_hour_anomaly"]["explanation"]

    assert "reply_to_divergence" in feats
    assert "consultant-mail.net" in feats["reply_to_divergence"]["explanation"]

    # every stored contribution sums (capped) to the total — explainability
    total = sum(f["contribution"] for f in feats.values())
    assert abs(min(total, 100.0) - mailbox.score_of(row_id)) < 1e-6
    assert mailbox.score_of(row_id) >= 25


def test_normal_message_from_same_sender_scores_zero(mailbox):
    _baseline(mailbox)
    normal = make_email(
        PEER,
        display="Sam Okafor",
        when=T0 + timedelta(days=63, hours=1),
        body="status update as usual, numbers attached below inline.",
    )
    row_id = mailbox.deliver(normal)
    assert mailbox.score_of(row_id) == 0.0
    assert mailbox.features_of(row_id) == {}


def test_novel_attachment_type_after_attachment_history(mailbox):
    _baseline(mailbox, n=12)
    # sender routinely sends PDFs
    for i in range(4):
        mailbox.deliver(
            make_email(
                PEER,
                display="Sam Okafor",
                when=T0 + timedelta(days=70 + i * 3),
                attachments=[("report.pdf", "application/pdf", b"%PDF-fake")],
            )
        )
    row_id = mailbox.deliver(
        make_email(
            PEER,
            display="Sam Okafor",
            when=T0 + timedelta(days=85),
            attachments=[("run_me.zip", "application/zip", b"PK\x03\x04y")],
        )
    )
    feats = mailbox.features_of(row_id)
    assert "attachment_type_novelty" in feats
    assert ".zip" in feats["attachment_type_novelty"]["explanation"]
    assert "first_attachment_ever" not in feats

    # and another PDF is NOT novel
    row_id2 = mailbox.deliver(
        make_email(
            PEER,
            display="Sam Okafor",
            when=T0 + timedelta(days=88),
            attachments=[("report.pdf", "application/pdf", b"%PDF-fake")],
        )
    )
    assert "attachment_type_novelty" not in mailbox.features_of(row_id2)


def test_thin_baseline_mutes_behavioral_features(mailbox):
    # only 3 prior messages: behavioral features must not fire at all
    mailbox.establish_contact(PEER, n=3)
    row_id = mailbox.deliver(
        make_email(
            PEER,
            when=T0 + timedelta(days=30),
            attachments=[("x.zip", "application/zip", b"PK")],
        )
    )
    feats = mailbox.features_of(row_id)
    assert "first_attachment_ever" not in feats
    assert "attachment_type_novelty" not in feats
