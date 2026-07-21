"""Dashboard: API endpoints, tier filter, TLS wiring in run()."""

from __future__ import annotations

import pytest
from conftest import make_email

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
    assert {"anomaly_score", "tier", "tier_label", "features", "message_id"} <= set(m)
    assert m["tier"] == 3  # current tier, not tier at scoring time


def test_messages_tier_filter(client, mailbox):
    mailbox.establish_contact("dana@acme.com")  # tier 3
    mailbox.deliver(make_email("cold@stranger.net"))  # tier 0
    tiers = {m["tier"] for m in client.get("/api/messages?tier=0").get_json()}
    assert tiers == {0}


def test_sender_endpoint(client, mailbox):
    mailbox.deliver(make_email("a@example.com"))
    assert client.get("/api/senders/1").status_code == 200
    assert client.get("/api/senders/999").status_code == 404


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
