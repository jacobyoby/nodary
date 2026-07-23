"""End-to-end CLI tests through main(argv), with storage routed to tmp via
NODARY_DB/NODARY_DB_KEY and the mail store via NODARY_MAIL_STORE."""

import pytest
from test_mail_store import store  # noqa: F401 (fixture reuse)

from nodary.cli import main

KEY = "ab" * 32


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NODARY_DB", str(tmp_path / "nodary.db"))
    monkeypatch.setenv("NODARY_DB_KEY", KEY)
    return tmp_path


def _add_account(monkeypatch, email="jacob@example.com"):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekrit")
    # never touch the real OS keyring from tests (also absent on CI runners)
    monkeypatch.setattr("nodary.cli.set_account_secret", lambda *_: None)
    return main(["add-account", email, "--host", "imap.example.com"])


def test_add_account_and_set_source(env, monkeypatch, capsys):
    assert _add_account(monkeypatch) == 0
    assert main(["set-source", "1", "mail-store"]) == 0
    assert "mail-store" in capsys.readouterr().out

    assert main(["set-source", "1", "imap", "--auth", "oauth2"]) == 0
    from nodary.cli import _open

    assert _open().execute("SELECT auth_method FROM accounts").fetchone()[0] == "oauth2"


def test_set_source_unknown_account(env, capsys):
    assert main(["set-source", "9", "mail-store"]) == 1


def test_sync_from_mail_store(env, monkeypatch, store, capsys):  # noqa: F811
    monkeypatch.setenv("NODARY_MAIL_STORE", str(store.root))
    assert _add_account(monkeypatch) == 0
    main(["set-source", "1", "mail-store"])
    assert main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "2 new messages" in out
    from nodary.cli import _open

    conn = _open()
    dirs = dict(
        conn.execute("SELECT from_email_norm, direction FROM messages").fetchall()
    )
    assert dirs["ada@example.com"] == "in"
    assert dirs["jacob@example.com"] == "out"  # sent folder, and it's me
    # incremental: nothing new on the second run
    assert main(["sync"]) == 0
    assert "0 new messages" in capsys.readouterr().out


def test_set_source_clears_stale_facts(env, monkeypatch, store, capsys):  # noqa: F811
    monkeypatch.setenv("NODARY_MAIL_STORE", str(store.root))
    _add_account(monkeypatch)
    main(["set-source", "1", "mail-store"])
    main(["sync"])
    from nodary.cli import _open

    assert _open().execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    # switching sources must not leave the old transport's facts behind
    main(["set-source", "1", "imap"])
    conn = _open()
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(last_seen_uid) FROM folders").fetchone()[0] == 0
