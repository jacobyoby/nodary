"""Read-only access to Apple Mail's on-disk message store (~/Library/Mail).

An alternative to IMAP for accounts macOS Mail already syncs — used when
direct IMAP is unavailable (e.g. Google Advanced Protection blocks app
passwords). Requires Full Disk Access. Never writes to the store.
"""

from .emlx import EmlxError, read_emlx
from .store import MailStore
from .transport import MailStoreTransport

__all__ = ["EmlxError", "MailStore", "MailStoreTransport", "read_emlx"]
