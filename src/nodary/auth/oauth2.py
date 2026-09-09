"""OAuth2 token refresh for Gmail and Microsoft 365.

Token endpoints are the ONLY non-IMAP network traffic nodary makes.
No other outbound HTTP is initiated by this module or the wider codebase.

Refresh tokens and access tokens live in the OS keychain (via
:mod:`nodary.storage.keys`), never in the SQLite database.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ..storage.keys import (
    get_refresh_token,
    set_account_secret,
    set_refresh_token,
)

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict[str, str]] = {
    "gmail": {
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "scopes": "https://mail.google.com/",
    },
    "m365": {
        "token_endpoint": (
            "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        ),
        "scopes": "https://outlook.office365.com/IMAP.AccessAsUser.All",
    },
}

# Maps well-known IMAP hosts to provider keys.
_HOST_PROVIDERS: dict[str, str] = {
    "imap.gmail.com": "gmail",
    "outlook.office365.com": "m365",
    "outlook.office.com": "m365",
}


class TokenRefreshError(Exception):
    """Raised when a token refresh attempt fails.

    Attributes:
        account_id: the account whose refresh failed
        reason: human-readable failure description
    """

    def __init__(self, account_id: int, reason: str):
        self.account_id = account_id
        self.reason = reason
        super().__init__(f"account #{account_id}: {reason}")


def detect_provider(imap_host: str) -> str | None:
    """Return the provider key for a given IMAP host, or None if unknown."""
    return _HOST_PROVIDERS.get(imap_host.lower())


def _get_client_credentials(
    provider: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[str, str | None]:
    """Resolve client_id and client_secret from arguments or environment.

    Environment variables:
        NODARY_GMAIL_CLIENT_ID / NODARY_GMAIL_CLIENT_SECRET
        NODARY_M365_CLIENT_ID / NODARY_M365_CLIENT_SECRET

    Raises TokenRefreshError if client_id cannot be resolved.
    """
    prefix = f"NODARY_{provider.upper()}_"
    resolved_id = client_id or os.environ.get(f"{prefix}CLIENT_ID")
    resolved_secret = client_secret or os.environ.get(f"{prefix}CLIENT_SECRET")
    if not resolved_id:
        raise TokenRefreshError(
            0,
            f"no client_id for provider {provider!r};"
            f" set {prefix}CLIENT_ID or pass client_id",
        )
    return resolved_id, resolved_secret


def refresh_access_token(
    account_id: int,
    provider: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Refresh the access token for *account_id* using the stored refresh token.

    POSTs to the provider's token endpoint with ``grant_type=refresh_token``.
    On success, the new access token is written to the OS keychain atomically
    (the old value is overwritten in place by ``set_account_secret``).

    Returns the new access token.

    Raises:
        TokenRefreshError: if no refresh token is stored, the HTTP request
            fails, or the response is missing an access token.
    """
    if provider not in PROVIDERS:
        raise TokenRefreshError(account_id, f"unknown provider {provider!r}")

    rt = get_refresh_token(account_id)
    if not rt:
        raise TokenRefreshError(
            account_id,
            "no refresh token in keychain;"
            " run `nodary set-secret <id> --refresh-token`",
        )

    cid, csecret = _get_client_credentials(provider, client_id, client_secret)

    cfg = PROVIDERS[provider]
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": cid,
        }
    ).encode()

    req = urllib.request.Request(
        cfg["token_endpoint"],
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode(errors="replace")[:200]
        raise TokenRefreshError(
            account_id,
            f"token endpoint returned HTTP {exc.code}: {detail}",
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TokenRefreshError(
            account_id, f"token endpoint unreachable: {exc}"
        ) from exc

    new_token = payload.get("access_token")
    if not new_token:
        raise TokenRefreshError(
            account_id,
            f"token endpoint response missing access_token: {payload!r}",
        )

    # Atomic update: keyring.set_password overwrites the existing entry.
    # If a new refresh token is rotated in, store it too.
    set_account_secret(account_id, new_token)
    new_rt = payload.get("refresh_token")
    if new_rt:
        set_refresh_token(account_id, new_rt)

    return new_token
