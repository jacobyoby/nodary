"""OAuth2 token management for IMAP providers."""

from .oauth2 import (
    PROVIDERS,
    TokenRefreshError,
    detect_provider,
    refresh_access_token,
)

__all__ = [
    "PROVIDERS",
    "TokenRefreshError",
    "detect_provider",
    "refresh_access_token",
]
