"""Fixture: lookalike-domain phish — a new sender mimics a trusted contact
via homoglyph domain + display name collision."""

from datetime import timedelta

from conftest import T0, make_email


def _trusted_dana(mailbox):
    mailbox.establish_contact("dana.ito@acme-corp.com", display="Dana Ito", n=10)
    assert mailbox.tier_of("dana.ito@acme-corp.com") == 3


def test_homoglyph_domain_and_name_collision(mailbox):
    _trusted_dana(mailbox)

    phish = make_email(
        "dana.ito@acme-c0rp.com",  # 0 for o
        display="Dana Ito",
        when=T0 + timedelta(days=40),
        body="please review the attached invoice: https://acme-pay.net/inv",
        attachments=[("invoice.zip", "application/zip", b"PK\x03\x04fake")],
    )
    row_id = mailbox.deliver(phish)
    feats = mailbox.features_of(row_id)

    assert "lookalike_domain" in feats
    assert "acme-corp.com" in feats["lookalike_domain"]["explanation"]
    assert feats["lookalike_domain"]["raw_value"] == 1.0  # skeleton collision

    assert "display_name_collision" in feats
    assert "dana.ito@acme-corp.com" in feats["display_name_collision"]["explanation"]

    # cold-contact context stacks on top
    assert "cold_attachment" in feats
    assert mailbox.score_of(row_id) >= 60


def test_edit_distance_lookalike(mailbox):
    _trusted_dana(mailbox)
    row_id = mailbox.deliver(
        make_email("it@acme-crop.com", when=T0 + timedelta(days=40))
    )
    feats = mailbox.features_of(row_id)
    assert "lookalike_domain" in feats
    assert 0 < feats["lookalike_domain"]["raw_value"] < 1.0


def test_own_trusted_domain_never_flagged_as_lookalike(mailbox):
    _trusted_dana(mailbox)
    # a different, legitimate sender at the SAME trusted domain
    row_id = mailbox.deliver(
        make_email("billing@acme-corp.com", when=T0 + timedelta(days=40))
    )
    assert "lookalike_domain" not in mailbox.features_of(row_id)


def test_unrelated_domain_not_flagged(mailbox):
    _trusted_dana(mailbox)
    row_id = mailbox.deliver(
        make_email("news@totally-different.org", when=T0 + timedelta(days=40))
    )
    assert "lookalike_domain" not in mailbox.features_of(row_id)
