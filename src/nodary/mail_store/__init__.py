"""Read-only access to Apple Mail's on-disk message store (~/Library/Mail).

An alternative to IMAP for accounts macOS Mail already syncs — used when
direct IMAP is unavailable (e.g. Google Advanced Protection blocks app
passwords). Requires Full Disk Access. Never writes to the store.
"""

from .emlx import EmlxError, read_emlx
from .store import (
    KNOWN_ROOTS,
    SUPPORTED_LAYOUTS,
    MailStore,
    MailStoreError,
    MailStoreLayoutError,
    detect_mail_store_root,
)
from .transport import MailStoreTransport

__all__ = [
    "EmlxError",
    "KNOWN_ROOTS",
    "MailStore",
    "MailStoreError",
    "MailStoreLayoutError",
    "MailStoreTransport",
    "SUPPORTED_LAYOUTS",
    "detect_mail_store_root",
    "read_emlx",
]
