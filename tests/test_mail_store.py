import plistlib
import sqlite3

import pytest

from nodary.imap_sync.bodystructure import walk
from nodary.imap_sync.sync import sync_account
from nodary.mail_store import (
    MailStore,
    MailStoreLayoutError,
    MailStoreTransport,
    detect_mail_store_root,
)

UUID = "AAAA1111-2222-3333-4444-555566667777"

MULTIPART = (
    b"From: Ada <ada@example.com>\r\n"
    b"To: jacob@example.com\r\n"
    b"Subject: report\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"see https://example.com/x\r\n"
    b"--B\r\n"
    b'Content-Type: application/pdf; name="q.pdf"\r\n'
    b'Content-Disposition: attachment; filename="q.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERg==\r\n"
    b"--B--\r\n"
)

SENT = (
    b"From: Jacob <jacob@example.com>\r\n"
    b"To: ada@example.com\r\n"
    b"Subject: re: report\r\n"
    b"\r\n"
    b"thanks\r\n"
)


def emlx_dir(root, folder_path, rowid):
    mbox = root / UUID
    for comp in folder_path.split("/"):
        mbox = mbox / f"{comp}.mbox"
    digits = str(rowid // 1000)
    sub = mbox / "instance-uuid" / "Data"
    if digits != "0":
        for d in reversed(digits):
            sub = sub / d
    sub = sub / "Messages"
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def write_emlx(root, folder_path, rowid, message, partial=False):
    suffix = ".partial.emlx" if partial else ".emlx"
    payload = plistlib.dumps({"flags": 0})
    (emlx_dir(root, folder_path, rowid) / f"{rowid}{suffix}").write_bytes(
        str(len(message)).encode() + b"\n" + message + payload
    )


def write_corrupt_emlx(root, folder_path, rowid, partial=False):
    suffix = ".partial.emlx" if partial else ".emlx"
    (emlx_dir(root, folder_path, rowid) / f"{rowid}{suffix}").write_bytes(
        b"this is not a valid emlx\n"
    )


def index_message(root, mailbox_rowid, rowid, sender=1):
    conn = sqlite3.connect(root / "MailData" / "Envelope Index")
    conn.execute(
        "INSERT INTO messages VALUES (?, ?, ?, 0)", (rowid, mailbox_rowid, sender)
    )
    conn.commit()
    conn.close()


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "V10"
    (root / "MailData").mkdir(parents=True)
    conn = sqlite3.connect(root / "MailData" / "Envelope Index")
    conn.executescript(
        """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY, mailbox INTEGER, sender INTEGER,
            deleted INTEGER DEFAULT 0);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT);
        """
    )
    conn.execute(f"INSERT INTO mailboxes VALUES (1, 'imap://{UUID}/INBOX')")
    conn.execute(f"INSERT INTO mailboxes VALUES (2, 'imap://{UUID}/Sent%20Messages')")
    conn.execute("INSERT INTO addresses VALUES (1, 'ada@example.com')")
    conn.execute("INSERT INTO addresses VALUES (2, 'jacob@example.com')")
    conn.execute("INSERT INTO messages VALUES (1201, 1, 1, 0)")
    conn.execute("INSERT INTO messages VALUES (1202, 2, 2, 0)")
    conn.execute("INSERT INTO messages VALUES (1203, 1, 1, 1)")  # deleted
    conn.commit()
    conn.close()
    write_emlx(root, "INBOX", 1201, MULTIPART)
    write_emlx(root, "Sent Messages", 1202, SENT, partial=True)
    return MailStore(root)


def test_detect_account(store):
    assert store.detect_account_uuid("jacob@example.com") == UUID
    assert store.detect_account_uuid("nobody@example.com") is None


def test_sync_folders_roles(store):
    assert store.sync_folders(UUID) == [
        ("INBOX", "inbox"),
        ("Sent Messages", "sent"),
    ]


def test_deleted_messages_excluded(store):
    t = MailStoreTransport(store, UUID)
    t.select_readonly("INBOX")
    assert t.new_uids(0) == [1201]


def test_fetch_meta_and_parts(store):
    t = MailStoreTransport(store, UUID)
    info = t.select_readonly("INBOX")
    assert info["uidnext"] == 1204
    fetched = t.fetch_meta([1201, 9999])  # 9999: indexed nowhere, no file
    assert set(fetched.messages) == {1201}
    assert fetched.permanent_failures == ()
    m = fetched.messages[1201]
    assert b"Subject: report" in m["header"]
    parts = walk(m["bodystructure"])
    assert [p.mime_type for p in parts] == ["text/plain", "application/pdf"]
    assert parts[1].is_attachment and parts[1].filename_ext == "pdf"
    text = t.fetch_part(1201, parts[0].section)
    assert b"https://example.com/x" in text
    assert parts[0].encoding == ""  # served decoded


def test_partial_emlx_headers_still_parse(store):
    t = MailStoreTransport(store, UUID)
    t.select_readonly("Sent Messages")
    fetched = t.fetch_meta([1202])
    assert b"Subject: re: report" in fetched.messages[1202]["header"]


def test_uidvalidity_tracks_mailbox_identity(store):
    """A recreated store (new mailbox rowids) must change uidvalidity so
    sync invalidates and refetches instead of silently missing low rowids."""
    t = MailStoreTransport(store, UUID)
    inbox = t.select_readonly("INBOX")
    sent = t.select_readonly("Sent Messages")
    assert inbox["uidvalidity"] != sent["uidvalidity"]


def test_missing_emlx_counted_as_transient(store):
    t = MailStoreTransport(store, UUID)
    t.select_readonly("INBOX")
    # 1203 is indexed but deleted=1 and has no file on disk
    fetched = t.fetch_meta([1201, 1203])
    assert set(fetched.messages) == {1201}
    assert fetched.permanent_failures == ()
    assert t.skipped_transient == 1
    assert t.skipped_permanent == 0


def test_fetch_meta_distinguishes_transient_and_permanent(store):
    index_message(store.root, mailbox_rowid=1, rowid=1204)
    index_message(store.root, mailbox_rowid=1, rowid=1205)
    write_corrupt_emlx(store.root, "INBOX", 1204)
    t = MailStoreTransport(store, UUID)
    t.select_readonly("INBOX")
    # 1203: absent file (transient); 1204: present but unparseable (permanent)
    fetched = t.fetch_meta([1201, 1203, 1204, 1205])
    assert set(fetched.messages) == {1201}
    assert [f.uid for f in fetched.permanent_failures] == [1204]
    assert fetched.permanent_failures[0].path is not None
    assert fetched.permanent_failures[0].path.endswith("1204.emlx")
    assert t.skipped_transient == 2  # 1203 absent, 1205 absent
    assert t.skipped_permanent == 1


def test_corrupt_emlx_does_not_block_later_messages(store, conn):
    """A permanently unparseable .emlx must not stall the high-water mark:
    the valid message after it is ingested on the first sync."""
    index_message(store.root, mailbox_rowid=1, rowid=1204)
    index_message(store.root, mailbox_rowid=1, rowid=1205)
    write_corrupt_emlx(store.root, "INBOX", 1204)
    write_emlx(store.root, "INBOX", 1205, MULTIPART)
    t = MailStoreTransport(store, UUID)
    stats = sync_account(conn, t, account_id=1)
    inbox_uids = [
        r["uid"]
        for r in conn.execute(
            "SELECT uid FROM messages m JOIN folders f ON f.id = m.folder_id"
            " WHERE f.name = 'INBOX' ORDER BY uid"
        )
    ]
    assert 1201 in inbox_uids
    assert 1204 not in inbox_uids
    assert 1205 in inbox_uids
    assert stats.new_messages == 3  # 1202 sent + 1201 + 1205
    last = conn.execute(
        "SELECT last_seen_uid FROM folders WHERE name = 'INBOX'"
    ).fetchone()["last_seen_uid"]
    assert last == 1205
    skipped = conn.execute(
        "SELECT uid, path, reason FROM skipped_messages ORDER BY uid"
    ).fetchall()
    assert [r["uid"] for r in skipped] == [1204]
    assert skipped[0]["path"].endswith("1204.emlx")
    assert skipped[0]["reason"]
    assert t.skipped_permanent == 1
    assert t.skipped_transient == 0


def test_missing_emlx_still_stops_high_water_mark(store, conn):
    """An indexed row with no file on disk is transient: stop and retry."""
    index_message(store.root, mailbox_rowid=1, rowid=1204)
    index_message(store.root, mailbox_rowid=1, rowid=1205)
    write_emlx(store.root, "INBOX", 1205, MULTIPART)
    t = MailStoreTransport(store, UUID)
    stats = sync_account(conn, t, account_id=1)
    inbox_uids = [
        r["uid"]
        for r in conn.execute(
            "SELECT uid FROM messages m JOIN folders f ON f.id = m.folder_id"
            " WHERE f.name = 'INBOX' ORDER BY uid"
        )
    ]
    assert inbox_uids == [1201]
    assert stats.new_messages == 2  # 1202 sent + 1201; 1205 blocked by gap
    last = conn.execute(
        "SELECT last_seen_uid FROM folders WHERE name = 'INBOX'"
    ).fetchone()["last_seen_uid"]
    assert last == 1201
    write_emlx(store.root, "INBOX", 1204, MULTIPART)
    t2 = MailStoreTransport(store, UUID)
    stats2 = sync_account(conn, t2, account_id=1)
    inbox_uids = [
        r["uid"]
        for r in conn.execute(
            "SELECT uid FROM messages m JOIN folders f ON f.id = m.folder_id"
            " WHERE f.name = 'INBOX' ORDER BY uid"
        )
    ]
    assert inbox_uids == [1201, 1204, 1205]
    assert stats2.new_messages == 2


def test_corrupt_partial_emlx_is_transient(store, conn):
    """A .partial.emlx that fails to parse is still an in-progress download."""
    index_message(store.root, mailbox_rowid=1, rowid=1204)
    index_message(store.root, mailbox_rowid=1, rowid=1205)
    write_corrupt_emlx(store.root, "INBOX", 1204, partial=True)
    write_emlx(store.root, "INBOX", 1205, MULTIPART)
    t = MailStoreTransport(store, UUID)
    sync_account(conn, t, account_id=1)
    last = conn.execute(
        "SELECT last_seen_uid FROM folders WHERE name = 'INBOX'"
    ).fetchone()["last_seen_uid"]
    assert last == 1201
    assert t.skipped_transient == 1
    assert t.skipped_permanent == 0
    assert conn.execute("SELECT COUNT(*) FROM skipped_messages").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Layout detection tests
# ---------------------------------------------------------------------------


def _make_layout(base, version: str, with_index: bool = True):
    """Create a layout directory under *base* with optional Envelope Index."""
    layout = base / version
    if with_index:
        maildata = layout / "MailData"
        maildata.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(maildata / "Envelope Index")
        conn.execute("CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT)")
        conn.execute(
            "CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, mailbox INTEGER,"
            " sender INTEGER, deleted INTEGER DEFAULT 0)"
        )
        conn.execute("CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT)")
        conn.commit()
        conn.close()
    else:
        layout.mkdir(parents=True, exist_ok=True)
    return layout


def test_detect_supported_layout(tmp_path, monkeypatch):
    """A supported layout (V10) with an Envelope Index stub is detected."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _make_layout(tmp_path / "Library" / "Mail", "V10")
    root = detect_mail_store_root()
    assert root.name == "V10"
    assert (root / "MailData" / "Envelope Index").is_file()


def test_detect_unsupported_layout(tmp_path, monkeypatch):
    """An unsupported layout (V11 only) fails with a clear error."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _make_layout(tmp_path / "Library" / "Mail", "V11")
    with pytest.raises(MailStoreLayoutError) as exc:
        detect_mail_store_root()
    err = exc.value
    assert err.found_version == "V11"
    assert "V11" in str(err)
    assert "unsupported" in str(err).lower()


def test_detect_multiple_versions_picks_newest_supported(tmp_path, monkeypatch):
    """When multiple layouts exist, the newest supported one is picked."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _make_layout(tmp_path / "Library" / "Mail", "V10")
    _make_layout(tmp_path / "Library" / "Mail", "V9")
    root = detect_mail_store_root()
    assert root.name == "V10"


def test_detect_no_mail_dir(tmp_path, monkeypatch):
    """No ~/Library/Mail/ directory fails with a clear error."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    with pytest.raises(MailStoreLayoutError) as exc:
        detect_mail_store_root()
    err = exc.value
    assert err.found_version is None
    assert "not found" in str(err).lower()


def test_detect_explicit_valid_path(tmp_path, monkeypatch):
    """Explicit configured_path to a valid V10 succeeds even without ~/Library/Mail."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    layout = _make_layout(tmp_path / "custom" / "mail", "V10")
    root = detect_mail_store_root(configured_path=str(layout))
    assert root == layout


def test_detect_explicit_parent_dir(tmp_path, monkeypatch):
    """Explicit configured_path to a parent dir containing V10 succeeds."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    parent = tmp_path / "custom" / "mail"
    _make_layout(parent, "V10")
    root = detect_mail_store_root(configured_path=str(parent))
    assert root.name == "V10"


def test_detect_explicit_bad_path(tmp_path, monkeypatch):
    """Explicit configured_path to a nonexistent path fails clearly."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    bad = tmp_path / "does" / "not" / "exist"
    with pytest.raises(MailStoreLayoutError) as exc:
        detect_mail_store_root(configured_path=str(bad))
    assert "does not exist" in str(exc.value).lower()


def test_detect_explicit_empty_dir(tmp_path, monkeypatch):
    """Explicit configured_path to an existing dir with no layout fails clearly."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    with pytest.raises(MailStoreLayoutError) as exc:
        detect_mail_store_root(configured_path=str(empty))
    assert "no recognised layout" in str(exc.value).lower()
