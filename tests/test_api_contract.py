"""API contract: error envelope + pagination (P0-2/P0-3, ADR 0001)."""

from __future__ import annotations

import pytest

from nodary.ui import create_app


@pytest.fixture
def client(conn):
    app = create_app(conn)
    app.config["TESTING"] = True
    return app.test_client()


def test_error_envelope_has_code(client):
    r = client.get("/api/status?account=999")
    assert r.status_code == 404
    j = r.get_json()
    assert "error" in j
    assert "code" in j
    assert j["code"] == "account_not_found"


def test_messages_invalid_tier_returns_400_with_code(client, mailbox):
    from conftest import make_email

    mailbox.deliver(make_email("a@example.com"))
    r = client.get("/api/messages?tier=zzz")
    assert r.status_code == 400
    j = r.get_json()
    assert j["code"] == "invalid_tier"


def test_messages_invalid_limit_returns_400(client, mailbox):
    from conftest import make_email

    mailbox.deliver(make_email("a@example.com"))
    for bad in ("abc", "-5", "0", "1001"):
        r = client.get(f"/api/messages?limit={bad}")
        assert r.status_code == 400, bad
        assert r.get_json()["code"] == "invalid_limit"


def test_messages_pagination_envelope(client, mailbox):
    from conftest import make_email

    mailbox.deliver(make_email("a@example.com"))
    r = client.get("/api/messages?limit=1").get_json()
    assert "data" in r and "pagination" in r
    assert r["pagination"]["limit"] == 1
    assert r["pagination"]["returned"] == len(r["data"])


def test_skipped_and_accounts_paginated(client, conn):

    # skipped empty still paginated
    s = client.get("/api/skipped").get_json()
    assert "data" in s and "pagination" in s
    # accounts paginated
    a = client.get("/api/accounts").get_json()
    assert "data" in a and "pagination" in a


def test_openapi_derived_from_schemas():
    import json
    import pathlib

    from nodary.ui.schemas import get_openapi_spec

    spec = get_openapi_spec()
    stored = json.loads(pathlib.Path("docs/specs/openapi.json").read_text())
    assert spec == stored

    # ErrorResponse must require code (ADR 0001)
    err = spec["components"]["schemas"]["ErrorResponse"]
    assert "code" in err["required"]
    # Paginated envelopes must exist
    assert "PaginatedMessages" in spec["components"]["schemas"]
    assert "PaginatedSkipped" in spec["components"]["schemas"]
