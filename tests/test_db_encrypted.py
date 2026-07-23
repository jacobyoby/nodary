"""The SQLCipher path used in production; the plain-SQLite fallback used by
the rest of the suite silently missed a Row-factory crash on this branch."""

import pytest

from nodary.storage import db as storage_db

pytestmark = pytest.mark.skipif(
    not storage_db.HAVE_SQLCIPHER, reason="sqlcipher3 not installed"
)

KEY = "ab" * 32


def test_connect_encrypted_roundtrip(tmp_path):
    path = tmp_path / "enc.db"
    conn = storage_db.connect(path, KEY)
    conn.execute(
        "INSERT INTO accounts (email, imap_host, imap_port, auth_method, created_at)"
        " VALUES ('a@b.c','imap.b.c',993,'app_password',0)"
    )
    conn.commit()
    # regression: Row access on a sqlcipher cursor (0.2.0 crashed here)
    row = conn.execute("SELECT * FROM accounts").fetchone()
    assert row["email"] == "a@b.c"
    assert (
        conn.execute("SELECT value FROM schema_meta WHERE key='encryption'").fetchone()[
            "value"
        ]
        == "sqlcipher"
    )
    conn.close()

    reopened = storage_db.connect(path, KEY)
    assert reopened.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    reopened.close()


def test_wrong_key_rejected(tmp_path):
    path = tmp_path / "enc.db"
    storage_db.connect(path, KEY).close()
    with pytest.raises(storage_db.sqlcipher3.dbapi2.DatabaseError):
        storage_db.connect(path, "cd" * 32)


def test_file_is_actually_encrypted(tmp_path):
    path = tmp_path / "enc.db"
    storage_db.connect(path, KEY).close()
    assert not path.read_bytes().startswith(b"SQLite format 3")
