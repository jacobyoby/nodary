"""Dashboard: API endpoints, tier filter, TLS wiring in run()."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

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
    assert {
        "id",
        "email",
        "auth_method",
        "last_error",
        "last_synced_at",
        "skip_count",
    } <= set(a)
    assert a["email"] == "jacob@myco.com"
    assert a["last_synced_at"] is None  # no sync yet


def test_status_skip_count_reflects_skipped_messages(client, conn):
    now = int(time.time())
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (1, 100, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (1, 101, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (1, 102, 'unparseable_header', ?)",
        (now,),
    )
    conn.commit()
    r = client.get("/api/status").get_json()
    assert r["total_skipped"] == 3
    assert r["accounts"][0]["skip_count"] == 3


def test_status_skip_count_isolated_per_account(client, conn):
    now = int(time.time())
    _add_second_account(conn)
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (1, 100, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (3, 200, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (3, 201, 'unparseable_header', ?)",
        (now,),
    )
    conn.commit()
    s1 = client.get("/api/status?account=1").get_json()
    s2 = client.get("/api/status?account=2").get_json()
    sall = client.get("/api/status?account=all").get_json()
    assert s1["total_skipped"] == 1
    assert s1["accounts"][0]["skip_count"] == 1
    assert s2["total_skipped"] == 2
    assert s2["accounts"][0]["skip_count"] == 2
    assert sall["total_skipped"] == 3


def test_status_last_error_surfaces(client, conn):
    conn.execute(
        "UPDATE accounts SET last_error = ? WHERE id = 1", ("missing credential",)
    )
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
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (1, 200, 'missing_emlx', ?)",
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


def test_skipped_endpoint_account_filter(client, conn):
    now = int(time.time())
    _add_second_account(conn)
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (1, 200, 'missing_emlx', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO skipped_messages (folder_id, uid, reason, skipped_at)"
        " VALUES (3, 201, 'unparseable_header', ?)",
        (now,),
    )
    conn.commit()
    only1 = client.get("/api/skipped?account=1").get_json()
    only2 = client.get("/api/skipped?account=2").get_json()
    both = client.get("/api/skipped?account=all").get_json()
    assert {r["uid"] for r in only1} == {200}
    assert {r["uid"] for r in only2} == {201}
    assert {r["uid"] for r in both} == {200, 201}
    assert client.get("/api/skipped?account=999").status_code == 404


# ── dashboard renders status strip ───────────────────────────────────


def test_index_renders_status_strip(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="status-strip"' in html
    assert "status-bar" in html
    assert "status-dot" in html


def test_index_renders_skip_overlay(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="skip-overlay"' in html
    assert "/api/skipped?account=" in html
    assert "UID / rowid" in html
    assert "account ${a.id}" in html
    assert "permanently skipped" in html


# ── account filter ────────────────────────────────────────────────────


def _add_second_account(conn):
    """Insert a second account + folder + identity, return (acct_id, folder_id)."""
    conn.execute(
        "INSERT INTO accounts (id, email, imap_host, auth_method, created_at)"
        " VALUES (2, 'alice@other.com', 'imap.other', 'app_password', 0)"
    )
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (2, 'alice@other.com')"
    )
    conn.execute(
        "INSERT INTO folders (id, account_id, name, role) VALUES (3, 2, 'INBOX', 'inbox')"
    )
    conn.commit()
    return 2, 3


def _deliver_into_folder(conn, folder_id, uid, from_addr, direction="in"):
    """Insert a minimal scored message directly into a folder (bypassing pipeline)."""
    conn.execute(
        "INSERT INTO senders (id, email_norm, domain, reg_domain,"
        " reg_domain_skeleton, is_freemail, first_seen_at, last_seen_at)"
        " VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM senders), ?, 'x.com', 'x.com', 'x.com', 0, 0, 0)"
        " ON CONFLICT(email_norm) DO NOTHING",
        (from_addr,),
    )
    sender_id = conn.execute(
        "SELECT id FROM senders WHERE email_norm = ?", (from_addr,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO messages (folder_id, uid, message_id, direction, sender_id,"
        " from_email_norm, sent_at, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 100)",
        (
            folder_id,
            uid,
            f"<test{uid}@x>",
            direction,
            sender_id,
            from_addr,
            1700000000 + uid,
        ),
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO message_scores (message_id, engine_version,"
        " trust_tier_at_scoring, baseline_n, anomaly_score, scored_at)"
        " VALUES (?, '1.0.0', 0, 1, 50.0, 0)",
        (msg_id,),
    )
    conn.commit()
    return msg_id


def test_messages_account_filter_returns_only_that_account(client, conn, mailbox):
    mailbox.deliver(make_email("cold@stranger.net"))  # account 1, folder 1
    _add_second_account(conn)
    _deliver_into_folder(conn, 3, 1, "other@x.com")  # account 2, folder 3

    msgs_acct1 = client.get("/api/messages?account=1").get_json()
    addrs1 = {m["from_email_norm"] for m in msgs_acct1}
    assert "cold@stranger.net" in addrs1
    assert "other@x.com" not in addrs1

    msgs_acct2 = client.get("/api/messages?account=2").get_json()
    addrs2 = {m["from_email_norm"] for m in msgs_acct2}
    assert "other@x.com" in addrs2
    assert "cold@stranger.net" not in addrs2


def test_messages_account_all_returns_all_accounts(client, conn, mailbox):
    mailbox.deliver(make_email("cold@stranger.net"))
    _add_second_account(conn)
    _deliver_into_folder(conn, 3, 1, "other@x.com")

    msgs = client.get("/api/messages?account=all").get_json()
    addrs = {m["from_email_norm"] for m in msgs}
    assert "cold@stranger.net" in addrs
    assert "other@x.com" in addrs


def test_messages_account_filter_composes_with_tier(client, conn, mailbox):
    mailbox.establish_contact("dana@acme.com")  # tier 3, account 1
    mailbox.deliver(make_email("cold@stranger.net"))  # tier 0, account 1
    _add_second_account(conn)
    _deliver_into_folder(conn, 3, 1, "other@x.com")  # tier 0, account 2

    # tier 0 + account 1: only cold@stranger.net
    msgs = client.get("/api/messages?account=1&tier=0").get_json()
    addrs = {m["from_email_norm"] for m in msgs}
    assert addrs == {"cold@stranger.net"}

    # tier 0 + account 2: only other@x.com
    msgs2 = client.get("/api/messages?account=2&tier=0").get_json()
    addrs2 = {m["from_email_norm"] for m in msgs2}
    assert addrs2 == {"other@x.com"}


def test_messages_account_not_found(client, conn):
    r = client.get("/api/messages?account=999")
    assert r.status_code == 404
    assert "error" in r.get_json()


def test_messages_account_invalid_id(client, conn):
    r = client.get("/api/messages?account=abc")
    assert r.status_code == 404


def test_status_account_filter_returns_only_that_account(client, conn, mailbox):
    mailbox.deliver(make_email("cold@stranger.net"))  # account 1
    _add_second_account(conn)
    _deliver_into_folder(conn, 3, 1, "other@x.com")  # account 2

    s1 = client.get("/api/status?account=1").get_json()
    assert len(s1["accounts"]) == 1
    assert s1["accounts"][0]["email"] == "jacob@myco.com"
    assert s1["incoming"] == 1

    s2 = client.get("/api/status?account=2").get_json()
    assert len(s2["accounts"]) == 1
    assert s2["accounts"][0]["email"] == "alice@other.com"
    assert s2["incoming"] == 1


def test_status_account_all_returns_all_accounts(client, conn, mailbox):
    mailbox.deliver(make_email("cold@stranger.net"))
    _add_second_account(conn)
    _deliver_into_folder(conn, 3, 1, "other@x.com")

    s = client.get("/api/status?account=all").get_json()
    emails = {a["email"] for a in s["accounts"]}
    assert emails == {"jacob@myco.com", "alice@other.com"}
    assert s["incoming"] == 2


def test_status_account_not_found(client, conn):
    r = client.get("/api/status?account=999")
    assert r.status_code == 404
    assert "error" in r.get_json()


def test_accounts_endpoint_lists_all(client, conn):
    _add_second_account(conn)
    accts = client.get("/api/accounts").get_json()
    emails = {a["email"] for a in accts}
    assert emails == {"jacob@myco.com", "alice@other.com"}
    assert all("id" in a for a in accts)


def test_index_renders_account_switcher(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="acct-switcher"' in html
    assert 'id="acct-select"' in html
    assert "/api/accounts" in html


# ── per-sender collapse ──────────────────────────────────────────────


def test_sender_collapse_one_row_per_sender(client, mailbox):
    """Two messages from the same sender collapse to one row."""
    t1 = T0 + timedelta(hours=1)
    t2 = T0 + timedelta(hours=2)
    mailbox.deliver(make_email("repeat@sender.com", when=t1))
    mailbox.deliver(make_email("repeat@sender.com", when=t2))
    mailbox.deliver(make_email("other@elsewhere.com"))

    msgs = client.get("/api/messages").get_json()
    addrs = [m["from_email_norm"] for m in msgs]
    assert len(addrs) == 2
    assert sorted(addrs) == ["other@elsewhere.com", "repeat@sender.com"]


def test_sender_collapse_sender_msg_count(client, mailbox):
    """sender_msg_count reflects total messages from that sender."""
    t1 = T0 + timedelta(hours=1)
    t2 = T0 + timedelta(hours=2)
    mailbox.deliver(make_email("repeat@sender.com", when=t1))
    mailbox.deliver(make_email("repeat@sender.com", when=t2))
    mailbox.deliver(make_email("solo@other.com"))

    msgs = client.get("/api/messages").get_json()
    by_addr = {m["from_email_norm"]: m for m in msgs}
    assert by_addr["repeat@sender.com"]["sender_msg_count"] == 2
    assert by_addr["solo@other.com"]["sender_msg_count"] == 1


def test_sender_collapse_rn_not_in_response(client, mailbox):
    """Internal _rn column must not leak into the JSON response."""
    t1 = T0 + timedelta(hours=1)
    t2 = T0 + timedelta(hours=2)
    mailbox.deliver(make_email("repeat@sender.com", when=t1))
    mailbox.deliver(make_email("repeat@sender.com", when=t2))

    msgs = client.get("/api/messages").get_json()
    for m in msgs:
        assert "_rn" not in m
        assert "_sender_msg_count" not in m


def test_sender_collapse_tier_filter(client, mailbox):
    """Tier filtering still works correctly with per-sender collapse."""
    mailbox.establish_contact("dana@acme.com")  # tier 3
    mailbox.deliver(make_email("dana@acme.com"))  # same sender, still tier 3
    mailbox.deliver(make_email("cold@stranger.net"))  # tier 0

    # tier 0 should return only cold@stranger.net (one row)
    msgs = client.get("/api/messages?tier=0").get_json()
    addrs = {m["from_email_norm"] for m in msgs}
    assert addrs == {"cold@stranger.net"}

    # tier 3 should return only dana@acme.com (collapsed to one row)
    msgs3 = client.get("/api/messages?tier=3").get_json()
    addrs3 = {m["from_email_norm"] for m in msgs3}
    assert addrs3 == {"dana@acme.com"}
    assert msgs3[0]["sender_msg_count"] >= 2


def test_sender_collapse_status_unique_incoming(client, mailbox):
    """unique_incoming counts distinct senders, matching the collapsed list."""
    t1 = T0 + timedelta(hours=1)
    t2 = T0 + timedelta(hours=2)
    mailbox.deliver(make_email("repeat@sender.com", when=t1))
    mailbox.deliver(make_email("repeat@sender.com", when=t2))
    mailbox.deliver(make_email("other@elsewhere.com"))

    s = client.get("/api/status").get_json()
    assert s["incoming"] == 3  # total messages
    assert s["unique_incoming"] == 2  # distinct senders
    assert s["senders"] == 2
