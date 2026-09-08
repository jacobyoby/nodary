"""Trust tier computation (DESIGN.md §5). First match wins, top-down."""

from __future__ import annotations

import sqlite3

from ..feature_extraction.profiles import ProfileSnapshot

TIER_LABELS = {
    3: "established",
    2: "prior one-way contact",
    1: "sender new, organization known",
    0: "never seen",
}

_ONE_WAY_MIN_MESSAGES = 2
_ONE_WAY_MIN_SPAN_SECONDS = 7 * 86400


def matching_tier_rule(
    conn: sqlite3.Connection, snap: ProfileSnapshot
) -> tuple[int, str]:
    """First matching rule, same order as DESIGN.md trust tiers.

    Returns ``(tier, rule)`` so the dashboard can show why a sender sits
    where they do without re-deriving the conditions in the UI layer.
    """
    if snap.n_replied_threads >= 1:
        return 3, "n_replied_threads >= 1 (user replied to a thread)"
    if snap.n_user_initiated >= 1:
        return 3, "n_user_initiated >= 1 (user started a thread)"
    # A 0 first/last-seen is the unknown-timestamp mark (or a legacy 1970
    # row), never a real span endpoint.
    first = snap.first_msg_at or 0
    last = snap.last_msg_at or 0
    if (
        snap.n_messages >= _ONE_WAY_MIN_MESSAGES
        and first > 0
        and last - first >= _ONE_WAY_MIN_SPAN_SECONDS
    ):
        return 2, "prior one-way contact spread over time"
    if not snap.is_freemail:
        dom = conn.execute(
            "SELECT n_replied_threads FROM domain_profiles WHERE reg_domain = ?",
            (snap.reg_domain,),
        ).fetchone()
        if dom and dom["n_replied_threads"] >= 1:
            return 1, "new sender, known non-freemail organization"
    return 0, "never seen or insufficient history"


def compute_tier(conn: sqlite3.Connection, snap: ProfileSnapshot) -> int:
    return matching_tier_rule(conn, snap)[0]


def store_tier(conn: sqlite3.Connection, sender_id: int, tier: int) -> None:
    conn.execute(
        "UPDATE sender_profiles SET trust_tier = ? WHERE sender_id = ?",
        (tier, sender_id),
    )
