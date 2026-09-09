"""Export/import profile round-trip and edge-case tests."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from nodary.cli import main
from nodary.storage import db as storage_db

KEY = "ab" * 32


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NODARY_DB", str(tmp_path / "nodary.db"))
    monkeypatch.setenv("NODARY_DB_KEY", KEY)
    return tmp_path


def _add_account(monkeypatch, email="jacob@example.com"):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sekrit")
    monkeypatch.setattr("nodary.cli.set_account_secret", lambda *_: None)
    return main(["add-account", email, "--host", "imap.example.com"])


class TestExportProfile:
    def test_export_creates_valid_archive(self, env, monkeypatch, capsys):
        _add_account(monkeypatch)
        archive = str(env / "export.tar.gz")
        assert main(["export-profile", "--output", archive]) == 0

        assert Path(archive).exists()
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names
            assert "nodary.db" in names

            # Extract and validate manifest
            tar.extractall(env / "extracted", filter="data")

        manifest = json.loads((env / "extracted" / "manifest.json").read_text())
        assert "schema_version" in manifest
        assert manifest["encryption_mode"] in ("plain", "sqlcipher")
        assert "engine_version" in manifest
        assert "psl_identity" in manifest
        assert "exported_at" in manifest
        assert "db_hash" in manifest
        assert "jacob@example.com" in manifest["psl_identity"]

    def test_export_no_db_fails(self, env, capsys):
        # Remove the DB (it was never created in this test)
        db_path = storage_db.default_db_path()
        if db_path.exists():
            db_path.unlink()
        archive = str(env / "export.tar.gz")
        assert main(["export-profile", "--output", archive]) == 1
        assert "no database found" in capsys.readouterr().err

    def test_export_include_secrets_adds_guidance(self, env, monkeypatch, capsys):
        _add_account(monkeypatch)
        archive = str(env / "export.tar.gz")
        assert main(["export-profile", "--output", archive, "--include-secrets"]) == 0

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            assert "keychain_guidance.txt" in names

    def test_export_hash_matches_db(self, env, monkeypatch):
        _add_account(monkeypatch)
        archive = str(env / "export.tar.gz")
        main(["export-profile", "--output", archive])

        db_path = storage_db.default_db_path()
        db_bytes = db_path.read_bytes()
        expected_hash = hashlib.sha256(db_bytes).hexdigest()

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(env / "extracted", filter="data")
        manifest = json.loads((env / "extracted" / "manifest.json").read_text())
        assert manifest["db_hash"] == expected_hash


class TestImportProfile:
    def _export(self, env, monkeypatch):
        _add_account(monkeypatch)
        archive = str(env / "export.tar.gz")
        main(["export-profile", "--output", archive])
        return archive

    def test_import_restores_db(self, env, monkeypatch, capsys):
        archive = self._export(env, monkeypatch)
        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 0
        assert Path(target).exists()

        # Verify the restored DB is valid
        conn = storage_db.connect(target)
        row = conn.execute("SELECT email FROM accounts").fetchone()
        assert row["email"] == "jacob@example.com"
        conn.close()

    def test_import_validates_hash(self, env, monkeypatch, capsys):
        archive = self._export(env, monkeypatch)

        # Corrupt the archive by modifying the DB inside
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmpdir, filter="data")
            db_file = Path(tmpdir) / "nodary.db"
            db_file.write_bytes(db_file.read_bytes() + b"CORRUPTED")
            corrupted = str(env / "corrupted.tar.gz")
            with tarfile.open(corrupted, "w:gz") as tar:
                tar.add(Path(tmpdir) / "manifest.json", arcname="manifest.json")
                tar.add(db_file, arcname="nodary.db")

        target = str(env / "restored.db")
        assert (
            main(["import-profile", "--input", corrupted, "--target-db", target]) == 1
        )
        assert "hash mismatch" in capsys.readouterr().err

    def test_import_refuses_overwrite_without_force(self, env, monkeypatch, capsys):
        archive = self._export(env, monkeypatch)
        target = str(env / "restored.db")

        # Create target file first
        Path(target).write_text("existing")

        assert main(["import-profile", "--input", archive, "--target-db", target]) == 1
        assert "already exists" in capsys.readouterr().err
        # File should be unchanged
        assert Path(target).read_text() == "existing"

    def test_import_force_overwrites(self, env, monkeypatch):
        archive = self._export(env, monkeypatch)
        target = str(env / "restored.db")
        Path(target).write_text("existing")

        assert (
            main(
                ["import-profile", "--input", archive, "--target-db", target, "--force"]
            )
            == 0
        )
        # Should now be a valid DB, not the original text
        conn = storage_db.connect(target)
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
        conn.close()

    def test_import_missing_archive(self, env, capsys):
        target = str(env / "restored.db")
        assert (
            main(
                [
                    "import-profile",
                    "--input",
                    str(env / "nonexistent.tar.gz"),
                    "--target-db",
                    target,
                ]
            )
            == 1
        )
        assert "archive not found" in capsys.readouterr().err

    def test_import_invalid_archive(self, env, capsys):
        bad_archive = str(env / "bad.tar.gz")
        Path(bad_archive).write_bytes(b"not a tarball")
        target = str(env / "restored.db")
        assert (
            main(["import-profile", "--input", bad_archive, "--target-db", target]) == 1
        )

    def test_import_missing_manifest(self, env, capsys):
        import tempfile

        archive = str(env / "no_manifest.tar.gz")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = Path(tmpdir) / "nodary.db"
            db_file.write_bytes(b"fake db")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(db_file, arcname="nodary.db")

        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 1
        assert "missing manifest.json" in capsys.readouterr().err

    def test_import_missing_db_in_archive(self, env, capsys):
        import tempfile

        archive = str(env / "no_db.tar.gz")
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.json"
            manifest.write_text('{"schema_version": "1"}')
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(manifest, arcname="manifest.json")

        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 1
        assert "missing nodary.db" in capsys.readouterr().err

    def test_import_manifest_missing_required_field(self, env, capsys):
        import tempfile

        archive = str(env / "bad_manifest.tar.gz")
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.json"
            manifest.write_text('{"schema_version": "1"}')  # missing other fields
            db_file = Path(tmpdir) / "nodary.db"
            db_file.write_bytes(b"fake")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(manifest, arcname="manifest.json")
                tar.add(db_file, arcname="nodary.db")

        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 1
        assert "missing" in capsys.readouterr().err


class TestEncryptionMismatch:
    def test_sqlcipher_archive_without_sqlcipher_fails(self, env, monkeypatch, capsys):
        """An archive with encryption_mode=sqlcipher fails when sqlcipher3
        is not installed."""
        # Create a valid archive, then tamper the manifest to say sqlcipher
        _add_account(monkeypatch)
        archive = str(env / "export.tar.gz")
        main(["export-profile", "--output", archive])

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmpdir, filter="data")
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["encryption_mode"] = "sqlcipher"
            manifest_path.write_text(json.dumps(manifest))
            tampered = str(env / "tampered.tar.gz")
            with tarfile.open(tampered, "w:gz") as tar:
                tar.add(manifest_path, arcname="manifest.json")
                tar.add(Path(tmpdir) / "nodary.db", arcname="nodary.db")

        target = str(env / "restored.db")
        if not storage_db.HAVE_SQLCIPHER:
            assert (
                main(["import-profile", "--input", tampered, "--target-db", target])
                == 1
            )
            assert "sqlcipher" in capsys.readouterr().err.lower()
        else:
            # With sqlcipher installed, the mismatch check for plain→sqlcipher
            # would trigger the other branch; skip this direction
            pytest.skip("sqlcipher is installed, cannot test missing-sqlcipher path")


class TestRoundTrip:
    def test_plain_round_trip(self, env, monkeypatch):
        """Export then import preserves database content."""
        if storage_db.HAVE_SQLCIPHER:
            pytest.skip("test covers plain mode only")

        _add_account(monkeypatch)

        # Add some data
        conn = storage_db.connect(storage_db.default_db_path(), KEY)
        conn.execute(
            "INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')"
        )
        conn.execute(
            "INSERT INTO messages"
            " (folder_id, uid, direction, from_email_norm, sent_at, size_bytes,"
            " n_attachments, n_links)"
            " VALUES (1, 1, 'in', 'sender@test.com', 1700000000, 1024, 0, 0)"
        )
        conn.commit()
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()

        archive = str(env / "export.tar.gz")
        assert main(["export-profile", "--output", archive]) == 0

        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 0

        restored = storage_db.connect(target)
        assert (
            restored.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == msg_count
        )
        assert (
            restored.execute("SELECT email FROM accounts").fetchone()["email"]
            == "jacob@example.com"
        )
        restored.close()

    def test_round_trip_preserves_accounts(self, env, monkeypatch):
        """Round trip preserves account data and user identities."""
        _add_account(monkeypatch)

        archive = str(env / "export.tar.gz")
        assert main(["export-profile", "--output", archive]) == 0

        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 0

        restored = storage_db.connect(target)
        acct = restored.execute("SELECT * FROM accounts").fetchone()
        assert acct["email"] == "jacob@example.com"
        assert acct["imap_host"] == "imap.example.com"

        identities = restored.execute(
            "SELECT email_norm FROM user_identities WHERE account_id = 1"
        ).fetchall()
        assert any(r["email_norm"] == "jacob@example.com" for r in identities)
        restored.close()


@pytest.mark.skipif(not storage_db.HAVE_SQLCIPHER, reason="sqlcipher3 not installed")
class TestEncryptedRoundTrip:
    def test_encrypted_round_trip(self, env, monkeypatch):
        """Export and import an encrypted database preserves content."""
        _add_account(monkeypatch)

        archive = str(env / "export.tar.gz")
        assert main(["export-profile", "--output", archive]) == 0

        # Verify the manifest says sqlcipher
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(env / "extracted", filter="data")
        manifest = json.loads((env / "extracted" / "manifest.json").read_text())
        assert manifest["encryption_mode"] == "sqlcipher"

        target = str(env / "restored.db")
        assert main(["import-profile", "--input", archive, "--target-db", target]) == 0

        # The restored DB should be openable with the same key
        restored = storage_db.connect(target, KEY)
        assert (
            restored.execute("SELECT email FROM accounts").fetchone()["email"]
            == "jacob@example.com"
        )
        restored.close()
