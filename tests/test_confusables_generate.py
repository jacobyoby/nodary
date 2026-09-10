"""The runtime confusables snapshot stays pinned to vendored UTS #39 data."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from nodary import confusables_generated as snapshot
from nodary.feature_extraction.normalize import skeleton

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / snapshot.SOURCE_PATH
GENERATOR = REPO_ROOT / "scripts" / "generate_confusables.py"


def test_snapshot_hash_matches_vendored_confusables_txt():
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert digest == snapshot.SOURCE_SHA256
    assert snapshot.UNICODE_SECURITY_VERSION == "17.0.0"


def test_generated_snapshot_is_current():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_snapshot_is_pure_data():
    text = Path(snapshot.__file__).read_text(encoding="utf-8")
    assert "urllib" not in text
    assert "http.client" not in text
    assert "requests" not in text
    assert "CONFUSABLES_MAP:" in text


def test_skeleton_uses_only_in_process_map():
    assert skeleton("paypal.com") == skeleton("pаypal.com")
    assert snapshot.CONFUSABLES_MAP["օ"] == "o"
    assert snapshot.CONFUSABLES_MAP["0"] == "o"
    assert snapshot.CONFUSABLES_MAP["m"] == "rn"
    # Uppercase UTS I→l must not poison Latin i.
    assert snapshot.CONFUSABLES_MAP.get("i", "i") == "i"
