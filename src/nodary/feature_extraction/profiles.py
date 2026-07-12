"""Sender/domain profile maintenance.

Profiles are derived caches over the `messages` fact table: every update here
is incremental, and pipeline.rebuild() can regenerate everything from facts.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .normalize import (
    address_domain,
    is_freemail,
    normalize_display_name,
    reg_domain,
    skeleton,
)
from .records import (
    HIST_DOWS,
    HIST_HOURS,
    MessageRecord,
    pack_hist,
    unpack_hist,
)

PROFILE_VERSION = 1


@dataclass
class ProfileSnapshot:
    """A sender's baseline *before* the message being scored."""

    sender_id: int
    email_norm: str
    reg_domain: str
    is_freemail: bool
    n_messages: int = 0
    n_replied_threads: int = 0
    n_user_initiated: int = 0
    trust_tier: int = 0
    hour_histogram: list[int] = field(default_factory=lambda: [0] * HIST_HOURS)
    log_size_mean: float | None = None
    log_size_m2: float | None = None
    links_mean: float | None = None
    links_m2: float | None = None
    n_with_attachments: int = 0
    n_with_links: int = 0
    first_msg_at: int | None = None
    last_msg_at: int | None = None
    attachment_types: set[tuple[str, str]] = field(default_factory=set)  # (ext, mime)
    link_domains: set[str] = field(default_factory=set)
    replyto_addrs: set[str] = field(default_factory=set)


def upsert_sender(conn: sqlite3.Connection, email_norm: str, seen_at: int) -> int:
    row = conn.execute(
        "SELECT id, first_seen_at, last_seen_at FROM senders WHERE email_norm = ?",
        (email_norm,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE senders SET first_seen_at = MIN(COALESCE(first_seen_at, ?), ?),"
            " last_seen_at = MAX(COALESCE(last_seen_at, 0), ?) WHERE id = ?",
            (seen_at, seen_at, seen_at, row["id"]),
        )
        return row["id"]
    domain = address_domain(email_norm)
    rd = reg_domain(domain)
    cur = conn.execute(
        "INSERT INTO senders (email_norm, domain, reg_domain, reg_domain_skeleton,"
        " is_freemail, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
        (email_norm, domain, rd, skeleton(rd), int(is_freemail(rd)), seen_at, seen_at),
    )
    return cur.lastrowid


def resolve_thread(conn: sqlite3.Connection, record: MessageRecord) -> tuple[int, int]:
    """Attach the message to a thread via References/In-Reply-To.
    Returns (thread_id, depth)."""
    candidates = ([record.in_reply_to] if record.in_reply_to else []) + list(
        reversed(record.references)
    )
    for ref in candidates:
        row = conn.execute(
            "SELECT thread_id, thread_depth FROM messages"
            " WHERE message_id = ? AND thread_id IS NOT NULL"
            " ORDER BY id LIMIT 1",
            (ref,),
        ).fetchone()
        if row:
            return row["thread_id"], row["thread_depth"] + 1
    # No known ancestor: root a new thread at the oldest referenced id, or at
    # this message itself.
    root = record.references[0] if record.references else record.message_id
    if root is not None:
        existing = conn.execute(
            "SELECT id FROM threads WHERE root_message_id = ?", (root,)
        ).fetchone()
        if existing:
            return existing["id"], 1 if record.references else 0
    cur = conn.execute("INSERT INTO threads (root_message_id) VALUES (?)", (root,))
    return cur.lastrowid, 1 if record.references else 0


def insert_message(
    conn: sqlite3.Connection,
    folder_id: int,
    uid: int,
    record: MessageRecord,
    sender_id: int | None,
    thread_id: int,
    thread_depth: int,
) -> int:
    cur = conn.execute(
        """INSERT INTO messages (folder_id, uid, message_id, direction, sender_id,
             from_email_norm, from_display_name, reply_to_email_norm,
             to_me_directly, n_recipients, sent_at, sent_hour_local,
             sent_dow_local, size_bytes, n_attachments, n_links,
             links_extracted, is_reply, thread_id, thread_depth,
             auth_spf, auth_dkim, auth_dmarc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            folder_id,
            uid,
            record.message_id,
            record.direction,
            sender_id,
            record.from_email_norm,
            record.from_display_name,
            record.reply_to_email_norm,
            int(record.to_me_directly),
            record.n_recipients,
            record.sent_at,
            record.sent_hour_local,
            record.sent_dow_local,
            record.size_bytes,
            record.n_attachments,
            record.n_links,
            int(record.links_extracted),
            int(record.is_reply),
            thread_id,
            thread_depth,
            record.auth_spf,
            record.auth_dkim,
            record.auth_dmarc,
        ),
    )
    message_row_id = cur.lastrowid
    for att in record.attachments:
        conn.execute(
            "INSERT INTO message_attachments (message_id, mime_type, extension,"
            " size_bytes) VALUES (?,?,?,?)",
            (message_row_id, att.mime_type, att.extension, att.size_bytes),
        )
    for rd, n in record.link_domains.items():
        conn.execute(
            "INSERT INTO message_link_domains (message_id, reg_domain, n)"
            " VALUES (?,?,?)",
            (message_row_id, rd, n),
        )
    return message_row_id


def load_snapshot(conn: sqlite3.Connection, sender_id: int) -> ProfileSnapshot:
    sender = conn.execute(
        "SELECT email_norm, reg_domain, is_freemail FROM senders WHERE id = ?",
        (sender_id,),
    ).fetchone()
    snap = ProfileSnapshot(
        sender_id=sender_id,
        email_norm=sender["email_norm"],
        reg_domain=sender["reg_domain"],
        is_freemail=bool(sender["is_freemail"]),
    )
    prof = conn.execute(
        "SELECT * FROM sender_profiles WHERE sender_id = ?", (sender_id,)
    ).fetchone()
    if prof:
        snap.n_messages = prof["n_messages"]
        snap.n_replied_threads = prof["n_replied_threads"]
        snap.n_user_initiated = prof["n_user_initiated"]
        snap.trust_tier = prof["trust_tier"]
        snap.hour_histogram = unpack_hist(prof["hour_histogram"], HIST_HOURS)
        snap.log_size_mean = prof["log_size_mean"]
        snap.log_size_m2 = prof["log_size_m2"]
        snap.links_mean = prof["links_mean"]
        snap.links_m2 = prof["links_m2"]
        snap.n_with_attachments = prof["n_with_attachments"]
        snap.n_with_links = prof["n_with_links"]
        snap.first_msg_at = prof["first_msg_at"]
        snap.last_msg_at = prof["last_msg_at"]
    snap.attachment_types = {
        (r["extension"], r["mime_type"])
        for r in conn.execute(
            "SELECT extension, mime_type FROM sender_attachment_types"
            " WHERE sender_id = ?",
            (sender_id,),
        )
    }
    snap.link_domains = {
        r["reg_domain"]
        for r in conn.execute(
            "SELECT reg_domain FROM sender_link_domains WHERE sender_id = ?",
            (sender_id,),
        )
    }
    snap.replyto_addrs = {
        r["email_norm"]
        for r in conn.execute(
            "SELECT email_norm FROM sender_replyto_addrs WHERE sender_id = ?",
            (sender_id,),
        )
    }
    return snap


def _welford(mean: float | None, m2: float | None, n_new: int, x: float):
    if mean is None or n_new == 1:
        return x, 0.0
    delta = x - mean
    mean += delta / n_new
    m2 = (m2 or 0.0) + delta * (x - mean)
    return mean, m2


def update_profile_incoming(
    conn: sqlite3.Connection,
    sender_id: int,
    record: MessageRecord,
    thread_is_new: bool,
    thread_depth: int,
) -> None:
    import math

    prof = conn.execute(
        "SELECT * FROM sender_profiles WHERE sender_id = ?", (sender_id,)
    ).fetchone()
    if prof is None:
        hour = [0] * HIST_HOURS
        dow = [0] * HIST_DOWS
        n = 1
        log_size_mean, log_size_m2 = math.log(max(record.size_bytes, 1)), 0.0
        links_mean, links_m2 = float(record.n_links), 0.0
        n_att = int(record.n_attachments > 0)
        n_lnk = int(record.n_links > 0)
        n_rt = int(record.reply_to_email_norm is not None)
        n_threads = 1
        first_at, last_at = record.sent_at, record.sent_at
        max_depth = thread_depth
    else:
        hour = unpack_hist(prof["hour_histogram"], HIST_HOURS)
        dow = unpack_hist(prof["dow_histogram"], HIST_DOWS)
        n = prof["n_messages"] + 1
        log_size_mean, log_size_m2 = _welford(
            prof["log_size_mean"],
            prof["log_size_m2"],
            n,
            math.log(max(record.size_bytes, 1)),
        )
        links_mean, links_m2 = _welford(
            prof["links_mean"], prof["links_m2"], n, float(record.n_links)
        )
        n_att = prof["n_with_attachments"] + int(record.n_attachments > 0)
        n_lnk = prof["n_with_links"] + int(record.n_links > 0)
        n_rt = prof["n_replyto_divergent"] + int(record.reply_to_email_norm is not None)
        n_threads = prof["n_threads"] + int(thread_is_new)
        first_at = min(prof["first_msg_at"] or record.sent_at, record.sent_at)
        last_at = max(prof["last_msg_at"] or 0, record.sent_at)
        max_depth = max(prof["max_thread_depth"], thread_depth)

    if record.sent_hour_local is not None:
        hour[record.sent_hour_local] += 1
    if record.sent_dow_local is not None:
        dow[record.sent_dow_local] += 1

    conn.execute(
        """INSERT INTO sender_profiles (sender_id, n_messages, n_threads,
             n_replied_threads, n_user_initiated, trust_tier, hour_histogram,
             dow_histogram, log_size_mean, log_size_m2, links_mean, links_m2,
             n_with_attachments, n_with_links, n_replyto_divergent,
             first_msg_at, last_msg_at, max_thread_depth, updated_at,
             profile_version)
           VALUES (?,?,?,0,0,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(sender_id) DO UPDATE SET
             n_messages=excluded.n_messages, n_threads=excluded.n_threads,
             hour_histogram=excluded.hour_histogram,
             dow_histogram=excluded.dow_histogram,
             log_size_mean=excluded.log_size_mean,
             log_size_m2=excluded.log_size_m2,
             links_mean=excluded.links_mean, links_m2=excluded.links_m2,
             n_with_attachments=excluded.n_with_attachments,
             n_with_links=excluded.n_with_links,
             n_replyto_divergent=excluded.n_replyto_divergent,
             first_msg_at=excluded.first_msg_at,
             last_msg_at=excluded.last_msg_at,
             max_thread_depth=excluded.max_thread_depth,
             updated_at=excluded.updated_at,
             profile_version=excluded.profile_version""",
        (
            sender_id,
            n,
            n_threads,
            pack_hist(hour),
            pack_hist(dow),
            log_size_mean,
            log_size_m2,
            links_mean,
            links_m2,
            n_att,
            n_lnk,
            n_rt,
            first_at,
            last_at,
            max_depth,
            int(time.time()),
            PROFILE_VERSION,
        ),
    )

    for att in record.attachments:
        conn.execute(
            """INSERT INTO sender_attachment_types
                 (sender_id, extension, mime_type, n, first_seen_at)
               VALUES (?,?,?,1,?)
               ON CONFLICT(sender_id, extension, mime_type)
               DO UPDATE SET n = n + 1""",
            (sender_id, att.extension, att.mime_type, record.sent_at),
        )
    for rd, cnt in record.link_domains.items():
        conn.execute(
            """INSERT INTO sender_link_domains (sender_id, reg_domain, n, first_seen_at)
               VALUES (?,?,?,?)
               ON CONFLICT(sender_id, reg_domain) DO UPDATE SET n = n + excluded.n""",
            (sender_id, rd, cnt, record.sent_at),
        )
    if record.reply_to_email_norm:
        conn.execute(
            """INSERT INTO sender_replyto_addrs (sender_id, email_norm, n)
               VALUES (?,?,1)
               ON CONFLICT(sender_id, email_norm) DO UPDATE SET n = n + 1""",
            (sender_id, record.reply_to_email_norm),
        )
    if record.from_display_name:
        name_norm = normalize_display_name(record.from_display_name)
        if name_norm:
            conn.execute(
                """INSERT INTO sender_display_names
                     (sender_id, name_norm, name_skeleton, n)
                   VALUES (?,?,?,1)
                   ON CONFLICT(sender_id, name_norm) DO UPDATE SET n = n + 1""",
                (sender_id, name_norm, skeleton(name_norm)),
            )


def update_domain_incoming(
    conn: sqlite3.Connection, rd: str, sent_at: int, new_sender: bool
) -> None:
    conn.execute(
        """INSERT INTO domain_profiles (reg_domain, n_senders, n_messages,
             n_replied_threads, is_freemail, first_seen_at, last_seen_at)
           VALUES (?,?,1,0,?,?,?)
           ON CONFLICT(reg_domain) DO UPDATE SET
             n_senders = n_senders + ?,
             n_messages = n_messages + 1,
             first_seen_at = MIN(COALESCE(first_seen_at, ?), ?),
             last_seen_at = MAX(COALESCE(last_seen_at, 0), ?)""",
        (
            rd,
            int(new_sender),
            int(is_freemail(rd)),
            sent_at,
            sent_at,
            int(new_sender),
            sent_at,
            sent_at,
            sent_at,
        ),
    )


def credit_reply(
    conn: sqlite3.Connection, thread_id: int, sender_id: int, initiated: bool
) -> bool:
    """Credit a two-way interaction once per (thread, sender).
    Returns True if this was a new credit."""
    cur = conn.execute(
        "INSERT INTO thread_reply_credits (thread_id, sender_id) VALUES (?,?)"
        " ON CONFLICT DO NOTHING",
        (thread_id, sender_id),
    )
    if cur.rowcount == 0:
        return False
    col = "n_user_initiated" if initiated else "n_replied_threads"
    conn.execute(
        f"""INSERT INTO sender_profiles (sender_id, {col}, hour_histogram,
              dow_histogram, updated_at, profile_version)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(sender_id) DO UPDATE SET {col} = {col} + 1""",
        (
            sender_id,
            pack_hist([0] * HIST_HOURS),
            pack_hist([0] * HIST_DOWS),
            int(time.time()),
            PROFILE_VERSION,
        ),
    )
    if not initiated:
        rd = conn.execute(
            "SELECT reg_domain FROM senders WHERE id = ?", (sender_id,)
        ).fetchone()["reg_domain"]
        conn.execute(
            "UPDATE domain_profiles SET n_replied_threads = n_replied_threads + 1"
            " WHERE reg_domain = ?",
            (rd,),
        )
    return True


def median_gap_seconds(
    conn: sqlite3.Connection, sender_id: int, before_ts: int
) -> float | None:
    """Median inter-arrival time for a sender, computed from facts on demand
    (only queried for the rare dormant-resurrection check). Bounded to
    messages strictly before `before_ts` so incremental scoring and
    rebuild-replay see the same history."""
    times = [
        r["sent_at"]
        for r in conn.execute(
            "SELECT sent_at FROM messages WHERE sender_id = ? AND direction='in'"
            " AND sent_at < ? ORDER BY sent_at",
            (sender_id, before_ts),
        )
    ]
    if len(times) < 3:
        return None
    gaps = sorted(b - a for a, b in zip(times, times[1:], strict=False))
    mid = len(gaps) // 2
    return float(gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2)
