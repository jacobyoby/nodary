import plistlib
import sqlite3

import pytest

from nodary.imap_sync.bodystructure import walk
from nodary.mail_store import MailStore, MailStoreTransport

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
    b"Content-Type: application/pdf; name=\"q.pdf\"\r\n"
    b"Content-Disposition: attachment; filename=\"q.pdf\"\r\n"
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


def write_emlx(root, folder_path, rowid, message, partial=False):
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
    suffix = ".partial.emlx" if partial else ".emlx"
    payload = plistlib.dumps({"flags": 0})
    (sub / f"{rowid}{suffix}").write_bytes(
        str(len(message)).encode() + b"\n" + message + payload
    )


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
    conn.execute(
        f"INSERT INTO mailboxes VALUES (2, 'imap://{UUID}/Sent%20Messages')"
    )
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
    meta = t.fetch_meta([1201, 9999])  # 9999: indexed nowhere, no file
    assert set(meta) == {1201}
    m = meta[1201]
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
    meta = t.fetch_meta([1202])
    assert b"Subject: re: report" in meta[1202]["header"]
