"""Dashboard: API endpoints, tier filter, TLS wiring in run()."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest
from conftest import ME, T0, make_email

from nodary.feature_extraction.records import HIST_HOURS
from nodary.ui import create_app
from nodary.ui.server import run


@pytest.fixture
def client(conn):
    app = create_app(conn)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_served_self_contained(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # single self-contained page: no external asset references
    for marker in ("http://", "https://", "src=", 'rel="stylesheet"'):
        assert marker not in html
    # the Apple Mail deep link and tier badges must survive redesigns
    assert "message://" in html
    assert "openmail" in html
    assert "tier t${m.tier}" in html
    # sender drill-down is part of the same self-contained page
    assert 'id="sender-view"' in html
    assert "send-hour histogram" in html
    assert "size and link-density baselines" in html
    assert "known attachment types" in html
    assert "known link domains" in html
    assert "known Reply-To set" in html
    assert "recent scored messages" in html
    assert "sender baseline" in html
    assert "#sender/" in html


def test_status_counts(client, mailbox):
    mailbox.deliver(make_email("a@example.com"))
    r = client.get("/api/status").get_json()
    assert r["messages"] == 1
    assert r["incoming"] == 1
    assert r["senders"] == 1


def test_messages_include_scores_and_current_tier(client, mailbox):
    mailbox.establish_contact("dana@acme.com", display="Dana Ito")
    msgs = client.get("/api/messages").get_json()
    assert msgs
    m = msgs[0]
    assert {
        "anomaly_score",
        "tier",
        "tier_label",
        "features",
        "message_id",
        "sender_id",
    } <= set(m)
    assert m["tier"] == 3  # current tier, not tier at scoring time
    assert m["sender_id"] == 1


def test_messages_tier_filter(client, mailbox):
    mailbox.establish_contact("dana@acme.com")  # tier 3
    mailbox.deliver(make_email("cold@stranger.net"))  # tier 0
    tiers = {m["tier"] for m in client.get("/api/messages?tier=0").get_json()}
    assert tiers == {0}


def test_messages_bad_params_fall_back_to_defaults(client, mailbox):
    mailbox.deliver(make_email("a@example.com"))
    assert client.get("/api/messages?limit=abc").status_code == 200
    assert client.get("/api/messages?limit=-5").status_code == 200
    assert client.get("/api/messages?tier=zzz").status_code == 200


def test_sender_endpoint(client, mailbox):
    mailbox.deliver(make_email("a@example.com"))
    assert client.get("/api/senders/1").status_code == 200
    assert client.get("/api/senders/999").status_code == 404


def _sender_id(mailbox, email: str) -> int:
    return mailbox.conn.execute(
        "SELECT id FROM senders WHERE email_norm = ?", (email,)
    ).fetchone()[0]


def test_sender_detail_baselines_and_recent_messages(client, mailbox):
    peer = "sam.okafor@partnerfirm.com"
    mailbox.establish_contact(peer, display="Sam Okafor", n=20)
    mailbox.deliver(
        make_email(
            peer,
            display="Sam Okafor",
            when=datetime(2026, 3, 20, 3, 12, tzinfo=UTC),
            body="urgent - wire details changed, see attached and confirm at "
            "https://secure-docs-verify.net/login/reset?token=abc",
            html='<a href="https://secure-docs-verify.net/login/reset?token=abc">'
            "reset</a>",
            attachments=[("payment_details.zip", "application/zip", b"PK\x03\x04x")],
            reply_to="sam.okafor@consultant-mail.net",
        )
    )
    sid = _sender_id(mailbox, peer)
    r = client.get(f"/api/senders/{sid}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["email_norm"] == peer
    assert data["display_name"] == "Sam Okafor"
    assert data["trust_tier"] == 3
    assert data["tier_label"] == "established"
    assert "n_replied_threads" in data["tier_rule"]
    assert data["n_messages"] >= 21
    assert data["span_seconds"] and data["span_seconds"] > 0
    assert data["n_replied_threads"] >= 1
    assert data["hour_histogram"] == list(data["hour_histogram"])
    assert len(data["hour_histogram"]) == HIST_HOURS
    assert all(isinstance(n, int) for n in data["hour_histogram"])
    assert data["hour_histogram"][3] >= 1  # 03:12 UTC attack
    assert data["size"]["typical_bytes"] is not None
    assert data["links"]["mean"] is not None
    assert any(
        a["extension"] == "zip" and a["mime_type"] == "application/zip"
        for a in data["attachment_types"]
    )
    assert any(
        d["reg_domain"] == "secure-docs-verify.net" for d in data["link_domains"]
    )
    assert any(
        rt["email_norm"] == "sam.okafor@consultant-mail.net" for rt in data["reply_to"]
    )
    assert data["recent_messages"]
    recent = data["recent_messages"][0]
    assert recent["sender_id"] == sid
    assert {f["feature"] for f in recent["features"]}
    assert "send_hour_anomaly" in {f["feature"] for f in recent["features"]}

    blob = json.dumps(data)
    # privacy: no subjects, filenames, full URLs, or body text
    assert "synthetic" not in blob
    assert "payment_details.zip" not in blob
    assert "https://secure-docs-verify.net" not in blob
    assert "/login/reset" not in blob
    assert "token=abc" not in blob
    assert "urgent - wire" not in blob
    assert "Subject" not in blob


def test_sender_detail_privacy_on_messages_list(client, mailbox):
    mailbox.deliver(
        make_email(
            "cold@stranger.net",
            body="please open https://phish.example/path?q=1",
            attachments=[("secret-invoice.pdf", "application/pdf", b"%PDF")],
        )
    )
    blob = json.dumps(client.get("/api/messages").get_json())
    assert "secret-invoice.pdf" not in blob
    assert "https://phish.example" not in blob
    assert "/path?q=1" not in blob
    assert "please open" not in blob
    assert "synthetic" not in blob


def test_sender_detail_user_initiated_rule(client, mailbox):
    peer = "newvendor@supplies.io"
    mailbox.send(make_email(ME, to=peer, when=T0))
    sid = _sender_id(mailbox, peer)
    data = client.get(f"/api/senders/{sid}").get_json()
    assert data["trust_tier"] == 3
    assert "n_user_initiated" in data["tier_rule"]
    assert data["recent_messages"] == []


class _CapturedRun:
    """Stub Flask.run capturing ssl_context."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs


def test_run_uses_tls_when_certificate_available(conn, monkeypatch, tmp_path):
    cert, key = tmp_path / "dashboard.pem", tmp_path / "dashboard-key.pem"
    cert.write_text("cert")
    key.write_text("key")
    monkeypatch.setattr(
        "nodary.ui.tls.ensure_certificate", lambda cert_dir=None: (cert, key)
    )
    captured = _CapturedRun()
    monkeypatch.setattr("flask.Flask.run", lambda self, **kw: captured(**kw))
    run(conn)
    assert captured.kwargs["ssl_context"] == (str(cert), str(key))
    assert captured.kwargs["host"] == "127.0.0.1"


def test_run_falls_back_to_http_without_certificate(conn, monkeypatch, capsys):
    monkeypatch.setattr("nodary.ui.tls.ensure_certificate", lambda cert_dir=None: None)
    captured = _CapturedRun()
    monkeypatch.setattr("flask.Flask.run", lambda self, **kw: captured(**kw))
    run(conn)
    assert captured.kwargs["ssl_context"] is None
    out = capsys.readouterr()
    assert "http://127.0.0.1" in out.out
    assert "mkcert" in out.err  # visible warning, not silent fallback


def test_run_no_tls_skips_certificate_lookup(conn, monkeypatch):
    def boom(cert_dir=None):  # pragma: no cover
        raise AssertionError("ensure_certificate must not be called with tls=False")

    monkeypatch.setattr("nodary.ui.tls.ensure_certificate", boom)
    captured = _CapturedRun()
    monkeypatch.setattr("flask.Flask.run", lambda self, **kw: captured(**kw))
    run(conn, tls=False)
    assert captured.kwargs["ssl_context"] is None


# ── sync-health status fields ─────────────────────────────────────────


def test_status_includes_accounts_and_skip_fields(client, conn):
    r = client.get("/api/status").get_json()
    assert "accounts" in r
    assert "total_skipped" in r
    assert "has_errors" in r
    assert isinstance(r["accounts"], list)
    assert r["total_skipped"] == 0
    assert r["has_errors"] is False


def test_status_accounts_per_account_shape(client, conn):
    r = client.get("/api/status").get_json()
    assert len(r["accounts"]) == 1  # conftest creates one account
    a = r["accounts"][0]
    assert {"id", "email", "auth_method", "last_error", "last_synced_at", "skip_count"} <= set(a)
    assert a["email"] == "jacob@myco.com"
    assert a["last_synced_at"] is None  # no sync yet


def test_status_skip_count_reflects_skipped_messages(client, conn):
    now = int(time.time())
    conn.execute(
        "INSERT INTO skipped_messages (account_id, folder_id, uid, reason, skipped_at)"
        " VALUES (1, 1, 100, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (account_id, folder_id, uid, reason, skipped_at)"
        " VALUES (1, 1, 101, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (account_id, folder_id, uid, reason, skipped_at)"
        " VALUES (1, 1, 102, 'unparseable_header', ?)",
        (now,),
    )
    conn.commit()
    r = client.get("/api/status").get_json()
    assert r["total_skipped"] == 3
    assert r["accounts"][0]["skip_count"] == 3


def test_status_last_error_surfaces(client, conn):
    conn.execute("UPDATE accounts SET last_error = ? WHERE id = 1", ("missing credential",))
    conn.commit()
    r = client.get("/api/status").get_json()
    assert r["has_errors"] is True
    assert r["accounts"][0]["last_error"] == "missing credential"


def test_status_last_synced_at_derived_from_folders(client, conn):
    ts = int(time.time()) - 3600
    conn.execute("UPDATE folders SET last_synced_at = ? WHERE id = 1", (ts,))
    conn.commit()
    r = client.get("/api/status").get_json()
    assert r["accounts"][0]["last_synced_at"] == ts


# ── skip list endpoint ───────────────────────────────────────────────


def test_skipped_endpoint_returns_rows(client, conn):
    now = int(time.time())
    conn.execute(
        "INSERT INTO skipped_messages (account_id, folder_id, uid, reason, skipped_at)"
        " VALUES (1, 1, 200, 'missing_emlx', ?)",
        (now,),
    )
    conn.commit()
    rows = client.get("/api/skipped").get_json()
    assert len(rows) == 1
    r = rows[0]
    assert r["uid"] == 200
    assert r["reason"] == "missing_emlx"
    assert r["folder_name"] == "INBOX"
    assert r["account_email"] == "jacob@myco.com"
    # no message content in skip list
    for key in ("body", "subject", "from_addr", "message_id"):
        assert key not in r


def test_skipped_endpoint_empty(client):
    rows = client.get("/api/skipped").get_json()
    assert rows == []


# ── dashboard renders status strip ───────────────────────────────────


def test_index_renders_status_strip(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="status-strip"' in html
    assert "status-bar" in html
    assert "status-dot" in html


def test_index_renders_skip_overlay(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="skip-overlay"' in html
    assert "/api/skipped" in html
