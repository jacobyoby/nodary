"""Locally-trusted TLS for the dashboard via mkcert.

Let's Encrypt cannot issue certificates for 127.0.0.1/localhost, and
requesting a public certificate would publish a hostname in Certificate
Transparency logs — the wrong tool for a privacy-first local app. mkcert
instead generates a certificate signed by a CA that exists only on this
machine. Certificate generation is entirely local: no network calls, no CT
log entries, nothing leaves the machine.

One-time setup (documented in README):

    brew install mkcert
    mkcert -install    # trust the local CA in the system store

Without mkcert (or an already-generated certificate) the dashboard falls
back to plain HTTP on 127.0.0.1 with a visible warning.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..storage.db import default_db_path

# Names the certificate is valid for. Loopback only — the dashboard never
# listens on a public interface (remote access, if ever wanted, goes through
# an SSH tunnel or Tailscale, not a public listener).
CERT_HOSTS = ("localhost", "127.0.0.1", "::1")

CERT_FILE = "dashboard.pem"
KEY_FILE = "dashboard-key.pem"


def default_cert_dir() -> Path:
    env = os.environ.get("NODARY_CERT_DIR")
    if env:
        return Path(env)
    return default_db_path().parent / "tls"


def find_certificate(cert_dir: Path) -> tuple[Path, Path] | None:
    """Return an existing (cert, key) pair, or None."""
    cert, key = cert_dir / CERT_FILE, cert_dir / KEY_FILE
    if cert.is_file() and key.is_file():
        return cert, key
    return None


def generate_certificate(cert_dir: Path) -> tuple[Path, Path] | None:
    """Generate a locally-trusted (cert, key) pair with mkcert, or return
    None when mkcert is not installed or fails."""
    mkcert = shutil.which("mkcert")
    if mkcert is None:
        return None
    cert_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cert_dir, 0o700)
    cert, key = cert_dir / CERT_FILE, cert_dir / KEY_FILE
    proc = subprocess.run(
        [mkcert, "-cert-file", str(cert), "-key-file", str(key), *CERT_HOSTS],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not (cert.is_file() and key.is_file()):
        print(
            f"nodary: mkcert failed ({proc.stderr.strip() or proc.returncode})",
            file=sys.stderr,
        )
        return None
    os.chmod(key, 0o600)
    return cert, key


def ensure_certificate(cert_dir: Path | None = None) -> tuple[Path, Path] | None:
    """Existing pair if present, else generate one; None when unavailable."""
    cert_dir = cert_dir or default_cert_dir()
    return find_certificate(cert_dir) or generate_certificate(cert_dir)
