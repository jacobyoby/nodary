"""Coverage for features the original suite never exercised:
link_density_anomaly, size_anomaly, dormant_resurrection (including its
only-alongside-another-flag invariant), embedded_addr_mismatch, and the
partial auth_fail verdicts."""

from datetime import timedelta

from conftest import T0, make_email


def test_link_density_anomaly_fires_on_link_heavy_shift(mailbox):
    peer = "alerts@vendor.io"
    mailbox.establish_contact(
        peer,
        n=10,
        body="scheduled report: https://vendor.io/report",
    )
    row_id = mailbox.deliver(
        make_email(
            peer,
            when=T0 + timedelta(days=100),
            message_id="<link-heavy@vendor.io>",
            body="new portal: " + " ".join(f"https://vendor.io/p{i}" for i in range(8)),
        )
    )
    feats = mailbox.features_of(row_id)
    assert "link_density_anomaly" in feats
    assert feats["link_density_anomaly"]["contribution"] == 5.0


def test_size_anomaly_fires_on_far_larger_message(mailbox):
    peer = "steady@vendor.io"
    mailbox.establish_contact(peer, n=10, body="short steady note")
    row_id = mailbox.deliver(
        make_email(
            peer,
            when=T0 + timedelta(days=40),
            message_id="<bulky@vendor.io>",
            body="x" * 5000,
        )
    )
    feats = mailbox.features_of(row_id)
    assert "size_anomaly" in feats
    assert feats["size_anomaly"]["contribution"] > 0


def test_dormant_resurrection_needs_another_flag(mailbox):
    peer = "old@partnerfirm.com"
    mailbox.establish_contact(peer, n=8, every_days=3)
    baseline_body = "status update as usual, numbers attached below inline."

    # 120 days quiet, then a message that also carries a first-ever
    # attachment: dormant fires alongside the other flag
    with_flag = mailbox.deliver(
        make_email(
            peer,
            when=T0 + timedelta(days=141),
            message_id="<return-flagged@partnerfirm.com>",
            body=baseline_body,
            attachments=[("notes.zip", "application/zip", b"PK\x03\x04x")],
        )
    )
    feats = mailbox.features_of(with_flag)
    assert "dormant_resurrection" in feats
    assert "first_attachment_ever" in feats

    # same long gap, but the message matches the baseline in every way:
    # dormant must NOT fire alone
    without_flag = mailbox.deliver(
        make_email(
            peer,
            when=T0 + timedelta(days=142),
            message_id="<return-quiet@partnerfirm.com>",
            body=baseline_body,
        )
    )
    assert mailbox.features_of(without_flag) == {}


def test_embedded_addr_mismatch_fires(mailbox):
    row_id = mailbox.deliver(
        make_email(
            "ap@payment-review.net",
            display='"Acme AP ap@acme-corp.com"',
            message_id="<embedded@payment-review.net>",
        )
    )
    feats = mailbox.features_of(row_id)
    assert feats["embedded_addr_mismatch"]["contribution"] == 10.0


def test_auth_fail_partial_verdicts(mailbox):
    both = mailbox.deliver(
        make_email(
            "x@bank-alerts.com",
            message_id="<auth-both@bank-alerts.com>",
            auth_results="mx.myco.com; dkim=fail; spf=fail",
        )
    )
    assert mailbox.features_of(both)["auth_fail"]["contribution"] == 12.0

    soft = mailbox.deliver(
        make_email(
            "y@bank-alerts.com",
            message_id="<auth-soft@bank-alerts.com>",
            auth_results="mx.myco.com; spf=softfail",
        )
    )
    assert mailbox.features_of(soft)["auth_fail"]["contribution"] == 6.0
