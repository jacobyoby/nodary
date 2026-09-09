"""Tests for PSL identity helper and drift detection."""

from __future__ import annotations

import re

from nodary.storage import db as storage_db
from nodary.storage.psl import get_current_psl_identity

# ---------------------------------------------------------------------------
# get_current_psl_identity()
# ---------------------------------------------------------------------------


def test_psl_identity_format():
    ident = get_current_psl_identity()
    assert ident.startswith("tldextract-")
    # Format: tldextract-{version}:{hash12}
    assert re.match(r"^tldextract-[\d.]+:[0-9a-f]{12}$", ident), ident


def test_psl_identity_stable():
    a = get_current_psl_identity()
    b = get_current_psl_identity()
    assert a == b


# ---------------------------------------------------------------------------
# drift detection via get_psl_drift_info()
# ---------------------------------------------------------------------------


def test_fresh_db_no_drift():
    conn = storage_db.connect(":memory:")
    info = storage_db.get_psl_drift_info(conn)
    assert info["drift"] is False
    assert info["current"] == info["stored"]
    # psl_version was inserted by the migration.
    stored = storage_db.get_meta(conn, "psl_version")
    assert stored == info["current"]
    conn.close()


def test_drift_detected_on_disk(tmp_path):
    path = tmp_path / "drift.db"
    conn = storage_db.connect(path)
    # Simulate a tldextract upgrade.
    storage_db.set_meta(conn, "psl_version", "tldextract-0.0.0:deadbeef0000")
    conn.commit()
    conn.close()

    conn2 = storage_db.connect(path)
    info = storage_db.get_psl_drift_info(conn2)
    assert info["drift"] is True
    assert info["stored"] == "tldextract-0.0.0:deadbeef0000"
    assert info["current"] == get_current_psl_identity()
    conn2.close()


def test_no_drift_after_rebuild(tmp_path):
    path = tmp_path / "rebuild.db"
    conn = storage_db.connect(path)
    storage_db.set_meta(conn, "psl_version", "tldextract-0.0.0:deadbeef0000")
    conn.commit()
    conn.close()

    # Simulate what cmd_rebuild does: update psl_version to current.
    conn2 = storage_db.connect(path)
    info = storage_db.get_psl_drift_info(conn2)
    assert info["drift"] is True
    storage_db.set_meta(conn2, "psl_version", get_current_psl_identity())
    conn2.commit()
    conn2.close()

    conn3 = storage_db.connect(path)
    info = storage_db.get_psl_drift_info(conn3)
    assert info["drift"] is False
    conn3.close()


# ---------------------------------------------------------------------------
# API status includes PSL fields
# ---------------------------------------------------------------------------


def test_api_status_includes_psl_fields(conn):
    from nodary.ui.server import create_app

    app = create_app(conn)
    with app.test_client() as client:
        resp = client.get("/api/status")
        data = resp.get_json()
    assert "psl_version" in data
    assert "psl_stored_version" in data
    assert "psl_drift" in data
    assert data["psl_drift"] is False


# ---------------------------------------------------------------------------
# Dashboard shows warning when drift detected
# ---------------------------------------------------------------------------


def test_dashboard_reports_drift(conn):
    # Simulate drift by writing a stale psl_version.
    storage_db.set_meta(conn, "psl_version", "tldextract-0.0.0:deadbeef0000")
    conn.commit()

    from nodary.ui.server import create_app

    app = create_app(conn)
    with app.test_client() as client:
        resp = client.get("/api/status")
        data = resp.get_json()
    assert data["psl_drift"] is True
