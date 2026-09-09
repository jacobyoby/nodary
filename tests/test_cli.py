"""End-to-end CLI tests through main(argv), with storage routed to tmp via
NODARY_DB/NODARY_DB_KEY and the mail store via NODARY_MAIL_STORE."""

import pytest
from test_mail_store import (  # noqa: F401 (fixture reuse)
    MULTIPART,
    index_message,
    store,
    write_corrupt_emlx,
    write_emlx,
)

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


def test_add_account_and_set_source(env, monkeypatch, capsys, store):  # noqa: F811
    assert _add_account(monkeypatch) == 0
    monkeypatch.setenv("NODARY_MAIL_STORE", str(store.root))
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


def test_status_reports_accounts_folders_and_db(env, monkeypatch, capsys):
    assert _add_account(monkeypatch) == 0
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "jacob@example.com" in out
    assert "engine v" in out
    assert "db encryption:" in out
    assert "no folders synced yet" in out

    from nodary.cli import _open

    conn = _open()
    conn.execute(
        "INSERT INTO folders (account_id, name, role, uidvalidity,"
        " last_seen_uid, last_synced_at) VALUES (1, 'INBOX', 'inbox', 7, 42,"
        " 1700000000)"
    )
    conn.commit()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "INBOX (inbox)" in out
    assert "uidvalidity=7" in out
    assert "last_seen_uid=42" in out
    assert "messages: 0" in out


def test_sync_reports_transient_and_permanent_skips_separately(
    env,
    monkeypatch,
    store,  # noqa: F811
    capsys,
):
    monkeypatch.setenv("NODARY_MAIL_STORE", str(store.root))
    # 1204 corrupt (permanent), 1205 missing (transient), 1206 valid but
    # behind the gap — first sync should report both skip kinds.
    index_message(store.root, mailbox_rowid=1, rowid=1204)
    index_message(store.root, mailbox_rowid=1, rowid=1205)
    index_message(store.root, mailbox_rowid=1, rowid=1206)
    write_corrupt_emlx(store.root, "INBOX", 1204)
    write_emlx(store.root, "INBOX", 1206, MULTIPART)
    assert _add_account(monkeypatch) == 0
    main(["set-source", "1", "mail-store"])
    assert main(["sync"]) == 0
    captured = capsys.readouterr()
    assert "not yet on disk and will be retried" in captured.err
    assert "corrupt or unparseable .emlx and were skipped" in captured.err
    from nodary.cli import _open

    conn = _open()
    inbox_uids = [
        r[0]
        for r in conn.execute(
            "SELECT uid FROM messages m JOIN folders f ON f.id = m.folder_id"
            " WHERE f.name = 'INBOX' ORDER BY uid"
        )
    ]
    assert inbox_uids == [1201]
    last = conn.execute(
        "SELECT last_seen_uid FROM folders WHERE name = 'INBOX'"
    ).fetchone()[0]
    assert last == 1204  # advanced past the corrupt file, stopped at the gap
    assert conn.execute("SELECT uid FROM skipped_messages").fetchone()[0] == 1204


def test_shared_alias_cannot_bind_two_accounts_to_one_store(
    env,
    monkeypatch,
    store,  # noqa: F811
    capsys,
):
    """Identities shared across accounts must not let a second account claim
    (and re-ingest) a store already bound to the first."""
    monkeypatch.setenv("NODARY_MAIL_STORE", str(store.root))
    _add_account(monkeypatch)  # jacob@example.com -> the store's account
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "x")
    main(
        [
            "add-account",
            "other@nowhere.example",
            "--host",
            "h",
            "--alias",
            "jacob@example.com",  # alias points at account 1's store
        ]
    )
    main(["set-source", "1", "mail-store"])
    main(["set-source", "2", "mail-store"])
    assert main(["sync"]) == 1  # account 2 must fail, not steal the store
    err = capsys.readouterr().err
    assert "no unclaimed account" in err
    from nodary.cli import _open

    n = _open().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 2  # only account 1's messages, ingested once


def test_sync_continues_after_first_account_missing_credential(
    env,
    monkeypatch,
    store,  # noqa: F811
    capsys,
):
    """A missing IMAP credential must not prevent later accounts from syncing."""
    monkeypatch.setenv("NODARY_MAIL_STORE", str(store.root))
    monkeypatch.setattr("nodary.cli.get_account_secret", lambda _id: None)
    assert _add_account(monkeypatch, "broken@example.com") == 0
    assert _add_account(monkeypatch, "jacob@example.com") == 0
    assert main(["set-source", "2", "mail-store"]) == 0

    assert main(["sync"]) == 1
    captured = capsys.readouterr()
    assert "account #1 (broken@example.com): no credential" in captured.err
    assert "2 new messages" in captured.out
    from nodary.cli import _open

    n = _open().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 2


def test_add_account_aborted_prompt_does_not_write_row(env, monkeypatch, capsys):
    def abort(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", abort)
    monkeypatch.setattr("nodary.cli.set_account_secret", lambda *_: None)
    assert main(["add-account", "x@example.com", "--host", "h"]) == 1
    assert "aborted" in capsys.readouterr().err
    from nodary.cli import _open

    assert _open().execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_set_source_mail_store_fails_on_bad_layout(env, monkeypatch, capsys, tmp_path):
    """set-source mail-store must validate the layout and refuse to switch
    when the store cannot be found."""
    assert _add_account(monkeypatch) == 0
    bad = tmp_path / "no-mail-here"
    monkeypatch.setenv("NODARY_MAIL_STORE", str(bad))
    assert main(["set-source", "1", "mail-store"]) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err.lower()
    # the account must NOT have been switched to mail_store
    from nodary.cli import _open

    method = (
        _open().execute("SELECT auth_method FROM accounts WHERE id = 1").fetchone()[0]
    )
    assert method != "mail_store"


def test_sync_fails_cleanly_on_missing_layout(env, monkeypatch, capsys, tmp_path):
    """sync must exit non-zero with a clear error when the mail store layout
    cannot be detected."""
    assert _add_account(monkeypatch) == 0
    # Pre-set the account to mail_store directly in the DB (bypassing CLI
    # validation, simulating a stale config or missing store after OS upgrade).
    from nodary.cli import _open

    conn = _open()
    conn.execute("UPDATE accounts SET auth_method = 'mail_store' WHERE id = 1")
    conn.commit()
    bad = tmp_path / "no-mail-here"
    monkeypatch.setenv("NODARY_MAIL_STORE", str(bad))
    assert main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err.lower()
