"""Privacy-invariant regressions.

After a synthetic sync that carries rich headers, body text, full URLs, and
named attachments, the database, on-disk DB bytes, and dashboard JSON must
omit subjects, raw body, filenames, full URLs, and credentials. Registrable
link domains and attachment extensions are the allowed residue.

These tests fail CI if a new column or extract path starts keeping forbidden
content. See SECURITY.md.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

from conftest import ME, T0, make_email
from fake_imap import FakeTransport

from nodary.cli import main
from nodary.imap_sync.sync import sync_account
from nodary.storage import db as storage_db
from nodary.storage.keys import set_account_secret
from nodary.ui import create_app

# Unique tokens planted in the synthetic message / credential. None of them
# is a registrable domain or an attachment extension, so a hit anywhere in
# persisted state or API JSON is a leak.
MARKER = "ZXQK9F3C"
SUBJECT = f"Q3 board packet {MARKER}-SUBJECT"
BODY = f"Please review the attached minutes {MARKER}-BODY before Friday."
FILENAME = f"q3-board-minutes-{MARKER}.pdf"
URL_HOST = "files.clicks-r-us.net"
URL_PATH = "/dl/EVIL"
URL_QUERY = f"token={MARKER}-TOKEN&file=minutes.pdf"
FULL_URL = f"https://{URL_HOST}{URL_PATH}?{URL_QUERY}"
HTML_HOST = "tracker.evil-analytics.net"
HTML_PATH = f"/c?id={MARKER}-CLICK"
FULL_HTML_URL = f"https://{HTML_HOST}{HTML_PATH}"
SECRET = f"NODARY-TEST-APP-PASSWORD-{MARKER}"

FORBIDDEN_SNIPPETS = (
    MARKER,
    SUBJECT,
    BODY,
    FILENAME,
    FULL_URL,
    FULL_HTML_URL,
    URL_PATH,
    f"{MARKER}-TOKEN",
    f"{MARKER}-CLICK",
    f"{MARKER}-SUBJECT",
    f"{MARKER}-BODY",
    "q3-board-minutes-",
)

# Column names that must not appear on any user table. `url` is listed
# because link tables store registrable domains only; filesystem `path` on
# skipped_messages is the documented mail-store exception and is not a URL.
_FORBIDDEN_COLUMN = re.compile(
    r"(^|_)(subject|body|html|filename|file_name|full_url|raw_url|url|href|"
    r"password|secret|token|credential|access_token|refresh_token)s?$",
    re.IGNORECASE,
)

_FORBIDDEN_JSON_KEYS = frozenset(
    {
        "subject",
        "body",
        "html",
        "filename",
        "file_name",
        "url",
        "full_url",
        "href",
        "password",
        "secret",
        "token",
        "credential",
        "raw_body",
        "attachment_filename",
    }
)

_LINK_TABLES = ("message_link_domains", "sender_link_domains")


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _all_text_cells(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    cells: list[tuple[str, str, str]] = []
    for table in _tables(conn):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            for key, value in dict(row).items():
                if isinstance(value, str):
                    cells.append((table, key, value))
                elif isinstance(value, bytes):
                    cells.append((table, key, value.decode("utf-8", errors="replace")))
    return cells


def _assert_no_snippets(haystack: str, where: str) -> None:
    lowered = haystack.lower()
    for snippet in FORBIDDEN_SNIPPETS:
        assert snippet.lower() not in lowered, f"{snippet!r} leaked into {where}"


def _json_keys(obj) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys.update(_json_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(_json_keys(item))
    return keys


def _seed_account(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO accounts (id, email, imap_host, auth_method, created_at)"
        " VALUES (1, ?, 'imap.test', 'app_password', 0)",
        (ME,),
    )
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (1, ?)", (ME,)
    )
    conn.execute(
        "INSERT INTO folders (id, account_id, name, role) VALUES"
        " (1, 1, 'INBOX', 'inbox'), (2, 1, 'Sent', 'sent')"
    )
    conn.commit()


def _rich_incoming():
    msg = make_email(
        "billing@vendor.io",
        display="Vendor Billing",
        when=T0,
        body=f"{BODY} download: {FULL_URL}",
        html=f'<a href="{FULL_HTML_URL}">click</a>',
        attachments=[(FILENAME, "application/pdf", b"%PDF-1.4 fake")],
        reply_to="billing-offline@consultant-mail.net",
        auth_results="mx.example.com; spf=pass; dkim=pass; dmarc=pass",
    )
    del msg["Subject"]
    msg["Subject"] = SUBJECT
    return msg


def _rich_outgoing(incoming):
    msg = make_email(
        ME,
        to="billing@vendor.io",
        when=T0 + timedelta(hours=2),
        in_reply_to=incoming["Message-ID"],
        references=[incoming["Message-ID"]],
        body="thanks, will review.",
    )
    del msg["Subject"]
    msg["Subject"] = f"Re: {SUBJECT}"
    return msg


def _sync_rich(conn: sqlite3.Connection) -> None:
    transport = FakeTransport()
    incoming = _rich_incoming()
    transport.add("INBOX", incoming)
    transport.add("Sent", _rich_outgoing(incoming))
    stats = sync_account(conn, transport, account_id=1)
    assert stats.new_messages == 2


def _db_file_bytes(path: Path) -> bytes:
    chunks = [path.read_bytes()]
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            chunks.append(extra.read_bytes())
    return b"".join(chunks)


def test_schema_has_no_forbidden_columns(conn):
    found: list[str] = []
    for table in _tables(conn):
        for column in _columns(conn, table):
            if _FORBIDDEN_COLUMN.search(column):
                found.append(f"{table}.{column}")
    assert found == []


def test_sync_persists_structure_not_content(tmp_path):
    path = tmp_path / "privacy.db"
    conn = storage_db.connect(path)
    _seed_account(conn)
    _sync_rich(conn)

    attachments = [dict(r) for r in conn.execute("SELECT * FROM message_attachments")]
    assert attachments
    assert {a["extension"] for a in attachments} == {"pdf"}
    assert {a["mime_type"] for a in attachments} == {"application/pdf"}

    link_domains = {
        r["reg_domain"]
        for r in conn.execute("SELECT reg_domain FROM message_link_domains")
    }
    assert link_domains == {"clicks-r-us.net", "evil-analytics.net"}
    for table in _LINK_TABLES:
        if table not in _tables(conn):
            continue
        for row in conn.execute(f"SELECT reg_domain FROM {table}"):
            value = row["reg_domain"]
            assert "/" not in value and "?" not in value and "://" not in value
            assert " " not in value and "@" not in value

    dump = "\n".join(
        f"{table}.{column}={value}" for table, column, value in _all_text_cells(conn)
    )
    _assert_no_snippets(dump, "SQL text columns")

    app = create_app(conn)
    client = app.test_client()
    messages = client.get("/api/messages").get_json()
    assert messages
    payload = {
        "messages": messages,
        "status": client.get("/api/status").get_json(),
        "sender": client.get(f"/api/senders/{messages[0]['sender_id']}").get_json(),
    }
    forbidden = _json_keys(payload) & _FORBIDDEN_JSON_KEYS
    assert forbidden == set()
    _assert_no_snippets(json.dumps(payload), "/api JSON")

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    _assert_no_snippets(
        _db_file_bytes(path).decode("utf-8", errors="replace"), "DB file bytes"
    )


def test_credentials_stay_out_of_the_database(tmp_path, monkeypatch):
    path = tmp_path / "nodary.db"
    monkeypatch.setenv("NODARY_DB", str(path))
    monkeypatch.setenv("NODARY_DB_KEY", "ab" * 32)
    ring: dict[tuple[str, str], str] = {}

    def _set_password(service, name, password):
        ring[(service, name)] = password

    monkeypatch.setattr("keyring.set_password", _set_password)
    monkeypatch.setattr(
        "keyring.get_password", lambda service, name: ring.get((service, name))
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET)

    assert main(["add-account", ME, "--host", "imap.example.com"]) == 0
    assert SECRET in ring.values()

    conn = storage_db.connect(path, "ab" * 32)
    for table in _tables(conn):
        for column in _columns(conn, table):
            assert not _FORBIDDEN_COLUMN.search(column), f"{table}.{column}"
    dump = "\n".join(value for _, _, value in _all_text_cells(conn))
    _assert_no_snippets(dump, "SQL text columns after add-account")
    conn.close()

    set_account_secret(1, SECRET)
    _assert_no_snippets(
        _db_file_bytes(path).decode("utf-8", errors="replace"), "DB file bytes"
    )
    assert SECRET.encode() not in _db_file_bytes(path)
