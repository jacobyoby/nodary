"""Turn raw message data into a MessageRecord.

Two entry points:
- record_from_message(): full email.message.Message (fixtures, tests, and the
  sync engine after selective part fetch).
- Header parsing helpers used by both.

Privacy: body text flows through extract_link_domains() and is dropped.
Subjects and filenames are read only to derive counts/extensions.
"""

from __future__ import annotations

import re
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from .normalize import (
    extract_link_domains,
    normalize_address,
)
from .records import AttachmentInfo, MessageRecord

MAX_TEXT_SCAN_BYTES = 1024 * 1024  # text parts larger than this skip link scan

_AUTH_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z0-9]+)", re.IGNORECASE)
_MSGID_RE = re.compile(r"<[^<>]+>")


def parse_auth_results(header_value: str) -> dict[str, str]:
    """Extract spf/dkim/dmarc verdicts from Authentication-Results."""
    results: dict[str, str] = {}
    for mech, verdict in _AUTH_RE.findall(header_value):
        mech = mech.lower()
        # keep the first (topmost, most recent hop) verdict per mechanism
        results.setdefault(mech, verdict.lower())
    return results


def parse_msgid_list(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return _MSGID_RE.findall(header_value)


def _sender_local_time(msg: Message) -> tuple[int | None, int | None, int | None]:
    """(utc_epoch, hour 0-23, dow 0-6) — hour/dow in the *sender's* UTC offset
    as carried by the Date header, so baselines track the sender's clock."""
    raw = msg.get("Date")
    if not raw:
        return None, None, None
    try:
        dt = parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        return None, None, None
    epoch = int(dt.timestamp())
    return epoch, dt.hour, dt.weekday()


def _walk_structure(msg: Message) -> tuple[list[AttachmentInfo], str, bool]:
    """Collect attachment structure and concatenated text for link scanning.
    Returns (attachments, text, fully_scanned)."""
    attachments: list[AttachmentInfo] = []
    texts: list[str] = []
    fully_scanned = True
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        is_attachment = disposition == "attachment" or (
            filename is not None and not ctype.startswith("text/")
        )
        if is_attachment:
            ext = ""
            if filename and "." in filename:
                ext = filename.rsplit(".", 1)[1].lower()[:16]
            payload = part.get_payload(decode=True)
            attachments.append(
                AttachmentInfo(
                    mime_type=ctype.lower(),
                    extension=ext,
                    size_bytes=len(payload) if payload else None,
                )
            )
        elif ctype in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if len(payload) > MAX_TEXT_SCAN_BYTES:
                fully_scanned = False
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                texts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                texts.append(payload.decode("utf-8", errors="replace"))
    return attachments, "\n".join(texts), fully_scanned


def record_from_headers(
    msg: Message,
    *,
    direction: str,
    my_addrs: frozenset[str],
    size_bytes: int,
    attachments: list[AttachmentInfo],
    link_text: str = "",
    links_extracted: bool = True,
) -> MessageRecord:
    """Sync-engine path: headers-only Message plus structure gathered from
    BODYSTRUCTURE and selectively fetched text parts."""
    rec = _record_from_headers_only(msg, direction=direction, my_addrs=my_addrs)
    rec.size_bytes = size_bytes
    rec.attachments = attachments
    rec.link_domains = extract_link_domains(link_text) if link_text else {}
    rec.links_extracted = links_extracted
    return rec


def record_from_message(
    msg: Message,
    *,
    direction: str,
    my_addrs: frozenset[str],
    size_bytes: int | None = None,
) -> MessageRecord:
    """Full-message path (fixtures, tests): walks MIME parts directly."""
    rec = _record_from_headers_only(msg, direction=direction, my_addrs=my_addrs)
    attachments, text, fully_scanned = _walk_structure(msg)
    rec.size_bytes = size_bytes if size_bytes is not None else len(bytes(msg))
    rec.attachments = attachments
    rec.link_domains = extract_link_domains(text) if text else {}
    rec.links_extracted = fully_scanned
    return rec


def _record_from_headers_only(
    msg: Message,
    *,
    direction: str,
    my_addrs: frozenset[str],
) -> MessageRecord:
    from_display, from_addr = parseaddr(msg.get("From", ""))
    from_norm = normalize_address(from_addr) if from_addr else ""

    reply_to_norm = None
    if msg.get("Reply-To"):
        _, rt_addr = parseaddr(msg["Reply-To"])
        if rt_addr:
            rt_norm = normalize_address(rt_addr)
            if rt_norm != from_norm:
                reply_to_norm = rt_norm

    recipients = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    rcpt_norm = [normalize_address(a) for _, a in recipients if a]
    to_only = [
        normalize_address(a) for _, a in getaddresses(msg.get_all("To", [])) if a
    ]
    to_me = any(a in my_addrs for a in to_only)

    epoch, hour, dow = _sender_local_time(msg)
    auth = parse_auth_results(msg.get("Authentication-Results", "") or "")

    refs = parse_msgid_list(msg.get("References"))
    in_reply_to_ids = parse_msgid_list(msg.get("In-Reply-To"))

    return MessageRecord(
        direction=direction,
        message_id=(parse_msgid_list(msg.get("Message-ID")) or [None])[0],
        from_email_norm=from_norm,
        from_display_name=from_display or None,
        reply_to_email_norm=reply_to_norm,
        in_reply_to=in_reply_to_ids[0] if in_reply_to_ids else None,
        references=refs,
        to_me_directly=to_me,
        n_recipients=len(rcpt_norm) or None,
        recipient_addrs_norm=rcpt_norm if direction == "out" else [],
        sent_at=epoch if epoch is not None else 0,
        sent_hour_local=hour,
        sent_dow_local=dow,
        size_bytes=0,
        auth_spf=auth.get("spf"),
        auth_dkim=auth.get("dkim"),
        auth_dmarc=auth.get("dmarc"),
    )
