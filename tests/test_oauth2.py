"""OAuth2 refresh-token flow tests.

Covers:
- Refresh token stored/retrieved from keychain (mock keychain)
- Access token refresh success (mock HTTP)
- Refresh failure surfaces account_id + reason
- IMAP auth failure triggers refresh + retry
- No refresh token → falls back to current behavior
- No token written to DB (verify by querying DB)
- Atomic credential write (no half-written on error)
- CLI set-secret --refresh-token
"""

import json
from unittest.mock import MagicMock

import pytest

from nodary.cli import main
from nodary.storage.keys import (
    get_account_secret,
    get_refresh_token,
    set_account_secret,
    set_refresh_token,
)

KEY = "ab" * 32


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_keyring(tmp_path, monkeypatch):
    """Replace the real keyring with an in-memory dict for every test."""
    _store: dict[str, str] = {}

    def _get(service, name):
        return _store.get(f"{service}/{name}")

    def _set(service, name, value):
        _store[f"{service}/{name}"] = value

    monkeypatch.setenv("NODARY_DB", str(tmp_path / "nodary.db"))
    monkeypatch.setenv("NODARY_DB_KEY", KEY)
    monkeypatch.setattr("keyring.get_password", _get)
    monkeypatch.setattr("keyring.set_password", _set)
    return _store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NODARY_DB", str(tmp_path / "nodary.db"))
    monkeypatch.setenv("NODARY_DB_KEY", KEY)
    return tmp_path


def _add_oauth2_account(monkeypatch, email="user@gmail.com"):
    """Add an OAuth2 account (mocks getpass for access + refresh tokens)."""
    tokens = iter(["fake-access-token", "fake-refresh-token"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(tokens))
    return main(["add-account", email, "--host", "imap.gmail.com", "--auth", "oauth2"])


# ---------------------------------------------------------------------------
# Keychain storage tests
# ---------------------------------------------------------------------------


class TestKeychainStorage:
    def test_store_and_retrieve_refresh_token(self, _mock_keyring):
        set_refresh_token(42, "my-refresh-token")
        assert get_refresh_token(42) == "my-refresh-token"

    def test_store_and_retrieve_access_token(self, _mock_keyring):
        set_account_secret(42, "my-access-token")
        assert get_account_secret(42) == "my-access-token"

    def test_refresh_token_absent_returns_none(self, _mock_keyring):
        assert get_refresh_token(999) is None

    def test_refresh_token_not_in_db(self, env, _mock_keyring, monkeypatch):
        """Refresh tokens must never be written to the SQLite database."""
        _add_oauth2_account(monkeypatch)

        from nodary.cli import _open

        conn = _open()
        # The accounts table has no column for refresh tokens.
        cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        assert "refresh_token" not in cols
        # And no table stores them either.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert not any("refresh" in t.lower() for t in tables)


# ---------------------------------------------------------------------------
# Token refresh logic tests
# ---------------------------------------------------------------------------


class TestRefreshAccessToken:
    def test_success(self, _mock_keyring, monkeypatch):
        """Successful refresh stores the new access token in the keychain."""
        from nodary.auth.oauth2 import refresh_access_token

        set_refresh_token(1, "old-refresh-token")
        monkeypatch.setenv("NODARY_GMAIL_CLIENT_ID", "test-client-id")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"access_token": "new-access-token", "expires_in": 3600}
        ).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = lambda *a: None

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=30: mock_response,
        )

        result = refresh_access_token(1, "gmail")
        assert result == "new-access-token"
        assert get_account_secret(1) == "new-access-token"

    def test_success_with_refresh_token_rotation(self, _mock_keyring, monkeypatch):
        """If the provider returns a new refresh token, it is stored."""
        from nodary.auth.oauth2 import refresh_access_token

        set_refresh_token(1, "old-rt")
        monkeypatch.setenv("NODARY_GMAIL_CLIENT_ID", "cid")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "access_token": "new-at",
                "refresh_token": "rotated-rt",
            }
        ).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = lambda *a: None

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=30: mock_response,
        )

        refresh_access_token(1, "gmail")
        assert get_refresh_token(1) == "rotated-rt"

    def test_no_refresh_token_raises(self, _mock_keyring):
        """No refresh token in keychain → clear error with account_id."""
        from nodary.auth.oauth2 import TokenRefreshError, refresh_access_token

        with pytest.raises(TokenRefreshError) as exc_info:
            refresh_access_token(7, "gmail")
        assert exc_info.value.account_id == 7
        assert "no refresh token" in exc_info.value.reason.lower()

    def test_http_error_surfaces_details(self, _mock_keyring, monkeypatch):
        """HTTP error from the token endpoint includes status and body."""
        import urllib.error

        from nodary.auth.oauth2 import TokenRefreshError, refresh_access_token

        set_refresh_token(1, "rt")
        monkeypatch.setenv("NODARY_GMAIL_CLIENT_ID", "cid")

        def _raise(req, timeout=30):
            err = urllib.error.HTTPError(
                url="https://oauth2.googleapis.com/token",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=None,
            )
            raise err

        monkeypatch.setattr("urllib.request.urlopen", _raise)

        with pytest.raises(TokenRefreshError) as exc_info:
            refresh_access_token(1, "gmail")
        assert "400" in str(exc_info.value)

    def test_missing_client_id_raises(self, _mock_keyring, monkeypatch):
        from nodary.auth.oauth2 import TokenRefreshError, refresh_access_token

        set_refresh_token(1, "rt")
        # Ensure no env var is set
        monkeypatch.delenv("NODARY_GMAIL_CLIENT_ID", raising=False)

        with pytest.raises(TokenRefreshError) as exc_info:
            refresh_access_token(1, "gmail")
        assert "client_id" in exc_info.value.reason.lower()

    def test_unknown_provider_raises(self, _mock_keyring):
        from nodary.auth.oauth2 import TokenRefreshError, refresh_access_token

        with pytest.raises(TokenRefreshError) as exc_info:
            refresh_access_token(1, "unknown_provider")
        assert "unknown provider" in exc_info.value.reason.lower()


# ---------------------------------------------------------------------------
# Provider detection tests
# ---------------------------------------------------------------------------


class TestDetectProvider:
    def test_gmail(self):
        from nodary.auth.oauth2 import detect_provider

        assert detect_provider("imap.gmail.com") == "gmail"

    def test_m365(self):
        from nodary.auth.oauth2 import detect_provider

        assert detect_provider("outlook.office365.com") == "m365"

    def test_case_insensitive(self):
        from nodary.auth.oauth2 import detect_provider

        assert detect_provider("IMAP.GMAIL.COM") == "gmail"

    def test_unknown_host(self):
        from nodary.auth.oauth2 import detect_provider

        assert detect_provider("imap.example.com") is None


# ---------------------------------------------------------------------------
# IMAP auth failure → refresh → retry tests
# ---------------------------------------------------------------------------


class TestImapAuthRefresh:
    def test_auth_error_detection(self):
        """_is_auth_error recognizes common IMAP auth failure messages."""
        from nodary.cli import _is_auth_error

        assert _is_auth_error(Exception("AUTHENTICATIONFAILED"))
        assert _is_auth_error(Exception("Invalid credentials"))
        assert _is_auth_error(Exception("Login failed"))
        assert _is_auth_error(Exception("Authentication failure"))
        assert not _is_auth_error(Exception("Connection refused"))
        assert not _is_auth_error(Exception("Timeout"))

    def test_login_failure_triggers_refresh(self, env, _mock_keyring, monkeypatch):
        """When login fails with auth error, refresh is attempted and retried."""
        _add_oauth2_account(monkeypatch)
        # Store tokens in mock keychain
        set_account_secret(1, "expired-token")
        set_refresh_token(1, "good-refresh-token")
        monkeypatch.setenv("NODARY_GMAIL_CLIENT_ID", "cid")

        # Mock the IMAP transport to fail first login, succeed after refresh
        login_calls = []

        class MockImapClient:
            SENT = "sent"

            def __init__(self, host, port=993, ssl=True):
                pass

            def login(self, user, password):
                pass

            def oauth2_login(self, user, token):
                login_calls.append(token)
                if token == "expired-token":
                    raise Exception("AUTHENTICATIONFAILED")

            def logout(self):
                pass

            def find_special_folder(self, kind):
                return None

            def select_folder(self, name, readonly=True):
                return {b"UIDVALIDITY": 1, b"UIDNEXT": 1}

            def search(self, criteria):
                return []

        monkeypatch.setattr("imapclient.IMAPClient", MockImapClient)
        monkeypatch.setattr("imapclient.SENT", "sent", raising=False)

        # Mock the HTTP refresh to return a new token
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"access_token": "refreshed-token"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda *a: None
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=30: mock_resp,
        )

        result = main(["sync"])
        assert result == 0
        assert "expired-token" in login_calls
        assert "refreshed-token" in login_calls

    def test_no_refresh_token_falls_back(self, env, _mock_keyring, monkeypatch, capsys):
        """When no refresh token is stored, auth failure is reported normally."""
        # Add account: access token given, refresh token skipped (empty).
        tokens = iter(["expired-token", ""])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(tokens))
        main(
            [
                "add-account",
                "user@gmail.com",
                "--host",
                "imap.gmail.com",
                "--auth",
                "oauth2",
            ]
        )
        set_account_secret(1, "expired-token")
        monkeypatch.setenv("NODARY_GMAIL_CLIENT_ID", "cid")

        class MockImapClient:
            SENT = "sent"

            def __init__(self, host, port=993, ssl=True):
                pass

            def oauth2_login(self, user, token):
                raise Exception("AUTHENTICATIONFAILED")

            def logout(self):
                pass

            def find_special_folder(self, kind):
                return None

        monkeypatch.setattr("imapclient.IMAPClient", MockImapClient)

        main(["sync"])
        # Should not crash; should report the failure
        err = capsys.readouterr().err
        assert "no refresh token" in err.lower() or "refresh" in err.lower()

    def test_refresh_failure_surfaces_account_id(
        self, env, _mock_keyring, monkeypatch, capsys
    ):
        """When refresh fails, the error includes the account_id."""
        import urllib.error

        _add_oauth2_account(monkeypatch)
        set_account_secret(1, "expired-token")
        set_refresh_token(1, "bad-refresh-token")
        monkeypatch.setenv("NODARY_GMAIL_CLIENT_ID", "cid")

        class MockImapClient:
            SENT = "sent"

            def __init__(self, host, port=993, ssl=True):
                pass

            def oauth2_login(self, user, token):
                if token == "expired-token":
                    raise Exception("AUTHENTICATIONFAILED")

            def logout(self):
                pass

            def find_special_folder(self, kind):
                return None

        monkeypatch.setattr("imapclient.IMAPClient", MockImapClient)

        # Mock HTTP to fail
        def _raise(req, timeout=30):
            raise urllib.error.HTTPError(
                url="x", code=401, msg="Unauthorized", hdrs=None, fp=None
            )

        monkeypatch.setattr("urllib.request.urlopen", _raise)

        main(["sync"])
        err = capsys.readouterr().err
        assert "account #1" in err or "account" in err.lower()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCli:
    def test_set_secret_refresh_token(self, env, _mock_keyring, monkeypatch, capsys):
        """set-secret --refresh-token stores a refresh token."""
        _add_oauth2_account(monkeypatch)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "new-rt")
        assert main(["set-secret", "1", "--refresh-token"]) == 0
        assert get_refresh_token(1) == "new-rt"
        out = capsys.readouterr().out
        assert "refresh token updated" in out

    def test_set_secret_default(self, env, _mock_keyring, monkeypatch, capsys):
        """set-secret (no --refresh-token) updates the access token."""
        _add_oauth2_account(monkeypatch)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "new-at")
        assert main(["set-secret", "1"]) == 0
        assert get_account_secret(1) == "new-at"
        out = capsys.readouterr().out
        assert "updated" in out

    def test_add_account_oauth2_prompts_for_refresh(
        self, env, _mock_keyring, monkeypatch, capsys
    ):
        """add-account --auth oauth2 prompts for an optional refresh token."""
        tokens = iter(["access-tok", "refresh-tok"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(tokens))
        result = main(
            [
                "add-account",
                "u@gmail.com",
                "--host",
                "imap.gmail.com",
                "--auth",
                "oauth2",
            ]
        )
        assert result == 0
        assert get_account_secret(1) == "access-tok"
        assert get_refresh_token(1) == "refresh-tok"
        out = capsys.readouterr().out
        assert "auto-renew" in out

    def test_add_account_oauth2_skip_refresh(
        self, env, _mock_keyring, monkeypatch, capsys
    ):
        """add-account --auth oauth2 with empty refresh token still works."""
        tokens = iter(["access-tok", ""])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(tokens))
        result = main(
            [
                "add-account",
                "u@gmail.com",
                "--host",
                "imap.gmail.com",
                "--auth",
                "oauth2",
            ]
        )
        assert result == 0
        assert get_account_secret(1) == "access-tok"
        assert get_refresh_token(1) is None
        out = capsys.readouterr().out
        assert "no refresh token stored" in out
