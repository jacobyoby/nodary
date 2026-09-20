"""DevTools contract smoke — optional dep gated (P2-3).

Tests run without Chrome; they assert the self-contained HTML contract that
chrome-devtools MCP will verify at runtime (screenshot, performance, a11y).
Skipped when chrome-devtools not configured — still documents the contract.
"""

from __future__ import annotations

import pytest

from nodary.ui import create_app


@pytest.fixture
def client(conn):
    app = create_app(conn)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_html_is_self_contained(client):
    html = client.get("/").get_data(as_text=True)
    # No external assets — required for offline dashboard
    for marker in ("http://", "https://", "src=", 'rel="stylesheet"'):
        assert marker not in html
    assert 'id="status-strip"' in html
    assert 'id="skip-overlay"' in html
    assert "fetchJson" in html  # P1-4 observability wrapper
    assert "unwrap" in html  # P0-3 pagination unwrap
    assert "console.debug" in html


def test_esc_handles_single_quote(client):
    html = client.get("/").get_data(as_text=True)
    # esc must handle ' (P2 XSS fix)
    assert "&#39;" in html or '\'":"&#39;"' in html


def test_mcp_json_exists_and_allows_local_only():
    import json
    import pathlib

    p = pathlib.Path(".mcp.json")
    assert p.exists(), ".mcp.json required per P2-2"
    cfg = json.loads(p.read_text())
    assert "chrome-devtools" in json.dumps(cfg)
    assert "127.0.0.1" in json.dumps(cfg)
    assert "isolated" in json.dumps(cfg)
