"""TLS certificate resolution: mkcert generation, reuse, graceful absence.

No real mkcert or network involved — the generator is stubbed. The real
mkcert path is also purely local (locally-trusted CA; no CT log entries).
"""

from __future__ import annotations

import os
import stat

from nodary.ui import tls


def test_default_cert_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NODARY_CERT_DIR", str(tmp_path / "certs"))
    assert tls.default_cert_dir() == tmp_path / "certs"


def test_default_cert_dir_sits_next_to_db(monkeypatch, tmp_path):
    monkeypatch.delenv("NODARY_CERT_DIR", raising=False)
    monkeypatch.setenv("NODARY_DB", str(tmp_path / "data" / "nodary.db"))
    assert tls.default_cert_dir() == tmp_path / "data" / "tls"


def test_existing_pair_is_reused_without_mkcert(monkeypatch, tmp_path):
    (tmp_path / tls.CERT_FILE).write_text("cert")
    (tmp_path / tls.KEY_FILE).write_text("key")
    # mkcert absent: reuse must not require it
    monkeypatch.setattr(tls.shutil, "which", lambda _: None)
    pair = tls.ensure_certificate(tmp_path)
    assert pair == (tmp_path / tls.CERT_FILE, tmp_path / tls.KEY_FILE)


def test_missing_mkcert_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(tls.shutil, "which", lambda _: None)
    assert tls.ensure_certificate(tmp_path) is None


def test_generation_invokes_mkcert_for_loopback_only(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # mkcert writes the files named by -cert-file/-key-file
        (tmp_path / tls.CERT_FILE).write_text("cert")
        (tmp_path / tls.KEY_FILE).write_text("key")

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(tls.shutil, "which", lambda _: "/usr/local/bin/mkcert")
    monkeypatch.setattr(tls.subprocess, "run", fake_run)

    pair = tls.ensure_certificate(tmp_path)
    assert pair == (tmp_path / tls.CERT_FILE, tmp_path / tls.KEY_FILE)
    (cmd,) = calls
    assert cmd[0] == "/usr/local/bin/mkcert"
    # certificate names: loopback only, never a public hostname
    assert set(cmd[-3:]) == set(tls.CERT_HOSTS)
    assert all(h in ("localhost", "127.0.0.1", "::1") for h in tls.CERT_HOSTS)


def test_generation_restricts_key_permissions(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        (tmp_path / tls.CERT_FILE).write_text("cert")
        (tmp_path / tls.KEY_FILE).write_text("key")

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(tls.shutil, "which", lambda _: "/usr/local/bin/mkcert")
    monkeypatch.setattr(tls.subprocess, "run", fake_run)

    _, key = tls.ensure_certificate(tmp_path)
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700


def test_mkcert_failure_returns_none(monkeypatch, tmp_path, capsys):
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stderr = "boom"

        return R()

    monkeypatch.setattr(tls.shutil, "which", lambda _: "/usr/local/bin/mkcert")
    monkeypatch.setattr(tls.subprocess, "run", fake_run)

    assert tls.ensure_certificate(tmp_path) is None
    assert "mkcert failed" in capsys.readouterr().err
