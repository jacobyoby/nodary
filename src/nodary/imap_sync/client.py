"""Read-only IMAP transport over imapclient.

Every folder is selected with readonly=True — nodary never sets flags, never
moves, never expunges, never sends. The transport interface is small so tests
can substitute a fake without touching sync logic.
"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol


class Transport(Protocol):
    def list_sync_folders(self) -> list[tuple[str, str]]: ...
    def select_readonly(self, name: str) -> dict[str, int]: ...
    def new_uids(self, after_uid: int) -> list[int]: ...
    def fetch_meta(self, uids: list[int]) -> dict[int, dict[str, Any]]: ...
    def fetch_part(self, uid: int, section: str) -> bytes: ...


class ImapTransport:
    def __init__(self, host: str, port: int = 993):
        import imapclient

        self._imapclient = imapclient
        self.client = imapclient.IMAPClient(host, port=port, ssl=True)

    def login_password(self, user: str, password: str) -> None:
        self.client.login(user, password)

    def login_oauth2(self, user: str, access_token: str) -> None:
        self.client.oauth2_login(user, access_token)

    def list_sync_folders(self) -> list[tuple[str, str]]:
        """INBOX plus the special-use Sent folder (required for tiers)."""
        folders = [("INBOX", "inbox")]
        sent = self.client.find_special_folder(self._imapclient.SENT)
        if sent:
            folders.append((sent, "sent"))
        return folders

    def select_readonly(self, name: str) -> dict[str, int]:
        info = self.client.select_folder(name, readonly=True)
        return {
            "uidvalidity": int(info[b"UIDVALIDITY"]),
            "uidnext": int(info.get(b"UIDNEXT", 0)),
        }

    def new_uids(self, after_uid: int) -> list[int]:
        uids = self.client.search(["UID", f"{after_uid + 1}:*"])
        # RFC quirk: 'N:*' matches the last message even when N > max UID.
        return sorted(u for u in uids if u > after_uid)

    def fetch_meta(self, uids: list[int]) -> dict[int, dict[str, Any]]:
        resp = self.client.fetch(
            uids, [b"BODY.PEEK[HEADER]", b"RFC822.SIZE", b"BODYSTRUCTURE"]
        )
        out: dict[int, dict[str, Any]] = {}
        for uid, data in resp.items():
            header = data.get(b"BODY[HEADER]", b"")
            out[uid] = {
                "header": header,
                "size": int(data.get(b"RFC822.SIZE", len(header))),
                "bodystructure": data.get(b"BODYSTRUCTURE"),
            }
        return out

    def fetch_part(self, uid: int, section: str) -> bytes:
        key = f"BODY[{section}]".encode()
        resp = self.client.fetch([uid], [f"BODY.PEEK[{section}]".encode()])
        return resp.get(uid, {}).get(key, b"") or b""

    def logout(self) -> None:
        with contextlib.suppress(Exception):
            self.client.logout()
