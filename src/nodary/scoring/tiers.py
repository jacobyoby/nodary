"""Trust tier computation (DESIGN.md §5). First match wins, top-down."""

from __future__ import annotations

import sqlite3

from ..feature_extraction.profiles import ProfileSnapshot

TIER_LABELS = {
    3: "established two-way correspondence",
    2: "prior one-way contact",
    1: "sender new, organization known",
    0: "never seen",
}

_ONE_WAY_MIN_MESSAGES = 2
_ONE_WAY_MIN_SPAN_SECONDS = 7 * 86400


def compute_tier(conn: sqlite3.Connection, snap: ProfileSnapshot) -> int:
    if snap.n_replied_threads >= 1 or snap.n_user_initiated >= 1:
        return 3
    if (
        snap.n_messages >= _ONE_WAY_MIN_MESSAGES
        and snap.first_msg_at is not None
        and snap.last_msg_at is not None
        and snap.last_msg_at - snap.first_msg_at >= _ONE_WAY_MIN_SPAN_SECONDS
    ):
        return 2
    if not snap.is_freemail:
        dom = conn.execute(
            "SELECT n_replied_threads FROM domain_profiles WHERE reg_domain = ?",
            (snap.reg_domain,),
        ).fetchone()
        if dom and dom["n_replied_threads"] >= 1:
            return 1
    return 0


def store_tier(conn: sqlite3.Connection, sender_id: int, tier: int) -> None:
    conn.execute(
        "UPDATE sender_profiles SET trust_tier = ? WHERE sender_id = ?",
        (tier, sender_id),
    )
