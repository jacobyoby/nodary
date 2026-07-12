"""Plain data types passed between sync, extraction, profiles, and scoring."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AttachmentInfo:
    mime_type: str
    extension: str  # lowercased, '' when unknown; filename itself is discarded
    size_bytes: int | None = None


@dataclass
class MessageRecord:
    """Everything nodary retains about one message. No body text, no subject,
    no filenames, no recipient list beyond what tier computation needs."""

    direction: str  # 'in' | 'out'
    from_email_norm: str
    sent_at: int  # UTC epoch seconds
    size_bytes: int
    message_id: str | None = None
    from_display_name: str | None = None
    reply_to_email_norm: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    to_me_directly: bool = False
    n_recipients: int | None = None
    recipient_addrs_norm: list[str] = field(default_factory=list)  # outgoing only
    sent_hour_local: int | None = None  # 0-23 in the SENDER's own UTC offset
    sent_dow_local: int | None = None  # 0=Mon .. 6=Sun, sender clock
    attachments: list[AttachmentInfo] = field(default_factory=list)
    link_domains: dict[str, int] = field(default_factory=dict)
    links_extracted: bool = True
    auth_spf: str | None = None
    auth_dkim: str | None = None
    auth_dmarc: str | None = None

    @property
    def n_attachments(self) -> int:
        return len(self.attachments)

    @property
    def n_links(self) -> int:
        return sum(self.link_domains.values())

    @property
    def is_reply(self) -> bool:
        return bool(self.in_reply_to or self.references)


HIST_HOURS = 24
HIST_DOWS = 7


def pack_hist(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}I", *values)


def unpack_hist(blob: bytes, n: int) -> list[int]:
    if not blob:
        return [0] * n
    return list(struct.unpack(f"<{n}I", blob))
