"""Database encryption key management.

The key is a random 256-bit value generated at first run and stored only in
the OS keychain (macOS Keychain / Secret Service / Windows Credential
Manager). It never touches disk. NODARY_DB_KEY overrides for tests/CI.
"""

from __future__ import annotations

import os
import secrets

SERVICE = "nodary"
DB_KEY_NAME = "db-key"


def get_or_create_db_key() -> str:
    """Return the hex-encoded database key, creating one if absent."""
    env = os.environ.get("NODARY_DB_KEY")
    if env:
        return env
    import keyring

    key = keyring.get_password(SERVICE, DB_KEY_NAME)
    if key is None:
        key = secrets.token_hex(32)
        keyring.set_password(SERVICE, DB_KEY_NAME, key)
    return key


def account_secret_name(account_id: int) -> str:
    return f"account/{account_id}"


def get_account_secret(account_id: int) -> str | None:
    import keyring

    return keyring.get_password(SERVICE, account_secret_name(account_id))


def set_account_secret(account_id: int, secret: str) -> None:
    import keyring

    keyring.set_password(SERVICE, account_secret_name(account_id), secret)
