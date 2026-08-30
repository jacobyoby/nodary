"""Database open/connect behavior that holds regardless of the sqlcipher extra."""

import pytest

from nodary.storage import db as storage_db


def test_db_file_is_owner_only(tmp_path):
    path = tmp_path / "x.db"
    storage_db.connect(path).close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_key_must_be_64_hex(tmp_path):
    with pytest.raises(ValueError):
        storage_db.connect(tmp_path / "y.db", key="not-hex")
    with pytest.raises(ValueError):
        storage_db.connect(tmp_path / "z.db", key="ab" * 31)  # 62 chars


def test_encryption_meta_repaired_when_stale(tmp_path):
    # a meta value frozen by an older install is corrected on the next open
    path = tmp_path / "m.db"
    conn = storage_db.connect(path)
    conn.execute("UPDATE schema_meta SET value='bogus' WHERE key='encryption'")
    conn.commit()
    conn.close()

    conn2 = storage_db.connect(path)  # keyless open uses the plain fallback
    assert storage_db.get_meta(conn2, "encryption") == "none"
    conn2.close()
