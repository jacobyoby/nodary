"""Fixture: cold outreach — never-seen senders, with and without payloads."""

from datetime import timedelta

from conftest import T0, make_email


def test_cold_sender_with_payload(mailbox):
    row_id = mailbox.deliver(
        make_email(
            "bd@growthly.io",
            display="Alex from Growthly",
            when=T0,
            body="book a demo: https://calendly-growthly.com/x and "
            "https://growthly.io/deck",
            attachments=[("deck.pdf", "application/pdf", b"%PDF")],
            reply_to="alex@growthly-mail.net",
        )
    )
    feats = mailbox.features_of(row_id)
    assert "cold_attachment" in feats
    assert "cold_links" in feats
    assert "cold_replyto" in feats
    assert "never-seen sender" in feats["cold_attachment"]["explanation"]
    assert mailbox.score_of(row_id) >= 20


def test_benign_cold_text_message_scores_zero(mailbox):
    row_id = mailbox.deliver(
        make_email(
            "old.friend@somewhere.org",
            when=T0,
            body="hey, long time! are you going to the reunion?",
        )
    )
    assert mailbox.score_of(row_id) == 0.0


def test_auth_fail_flags_any_tier(mailbox):
    row_id = mailbox.deliver(
        make_email(
            "notice@bank-alerts.com",
            when=T0,
            auth_results="mx.myco.com; spf=fail smtp.mailfrom=bank-alerts.com;"
            " dkim=fail; dmarc=fail",
        )
    )
    feats = mailbox.features_of(row_id)
    assert feats["auth_fail"]["raw_value"] == 1.0
    assert "DMARC" in feats["auth_fail"]["explanation"]


def test_cold_features_do_not_fire_for_established_contact(mailbox):
    peer = "dana@acme-corp.com"
    mailbox.establish_contact(peer, n=10)
    row_id = mailbox.deliver(
        make_email(
            peer,
            when=T0 + timedelta(days=40),
            body="see https://acme-corp.com/report",
        )
    )
    feats = mailbox.features_of(row_id)
    assert "cold_links" not in feats
    assert "cold_attachment" not in feats
