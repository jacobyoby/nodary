"""Deterministic, explainable scoring (DESIGN.md §6).

Every score is a sum of named feature contributions; the stored feature rows
ARE the explanation. Same message + same profile state => same score.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass

from ..feature_extraction.normalize import (
    address_domain,
    emails_in_text,
    normalize_display_name,
    osa_distance,
    reg_domain,
    skeleton,
)
from ..feature_extraction.profiles import ProfileSnapshot, median_gap_seconds
from ..feature_extraction.records import MessageRecord
from . import registry as R


@dataclass(frozen=True)
class FeatureResult:
    name: str
    raw: float  # 0..1
    explanation: str

    @property
    def weight(self) -> float:
        return R.FEATURES[self.name].weight

    @property
    def contribution(self) -> float:
        return self.raw * self.weight


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _fmt_size(n_bytes: int) -> str:
    if n_bytes >= 1024 * 1024:
        return f"{n_bytes / (1024 * 1024):.1f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes} B"


def _fire(results: list[FeatureResult], name: str, raw: float, expl: str) -> None:
    raw = _clamp(raw)
    if raw > 0.0:
        results.append(FeatureResult(name, round(raw, 4), expl))


# --------------------------------------------------------------- Group A ----


def _trusted_domains(conn: sqlite3.Connection, exclude_rd: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT DISTINCT s.reg_domain, s.reg_domain_skeleton
           FROM senders s JOIN sender_profiles p ON p.sender_id = s.id
           WHERE p.trust_tier >= 2 AND s.is_freemail = 0
             AND s.reg_domain != ?""",
        (exclude_rd,),
    ).fetchall()


def _identity_features(
    conn: sqlite3.Connection,
    record: MessageRecord,
    snap: ProfileSnapshot,
    tier: int,
    out: list[FeatureResult],
) -> None:
    sender_rd = snap.reg_domain

    # lookalike_domain — skip when the sender's own domain is itself trusted.
    own_trusted = conn.execute(
        """SELECT 1 FROM senders s JOIN sender_profiles p ON p.sender_id = s.id
           WHERE s.reg_domain = ? AND p.trust_tier >= 2 LIMIT 1""",
        (sender_rd,),
    ).fetchone()
    if not own_trusted and len(sender_rd) >= R.LOOKALIKE_MIN_LEN:
        sk = skeleton(sender_rd)
        best_raw, best_target = 0.0, None
        for row in _trusted_domains(conn, sender_rd):
            if len(row["reg_domain"]) < R.LOOKALIKE_MIN_LEN:
                continue
            if row["reg_domain_skeleton"] == sk:
                raw = 1.0
            else:
                d = osa_distance(
                    sk, row["reg_domain_skeleton"], cap=R.LOOKALIKE_MAX_DIST + 1
                )
                raw = {1: 0.9, 2: 0.6}.get(d, 0.0)
            if raw > best_raw:
                best_raw, best_target = raw, row["reg_domain"]
        if best_target:
            _fire(
                out,
                "lookalike_domain",
                best_raw,
                f"domain '{sender_rd}' resembles known domain '{best_target}'",
            )

    # display_name_collision
    if record.from_display_name:
        name_norm = normalize_display_name(record.from_display_name)
        if len(name_norm) >= 4:
            hit = conn.execute(
                """SELECT s.email_norm FROM sender_display_names dn
                   JOIN sender_profiles p ON p.sender_id = dn.sender_id
                   JOIN senders s ON s.id = dn.sender_id
                   WHERE dn.name_skeleton = ? AND p.trust_tier = 3
                     AND dn.sender_id != ? LIMIT 1""",
                (skeleton(name_norm), snap.sender_id),
            ).fetchone()
            if hit:
                _fire(
                    out,
                    "display_name_collision",
                    1.0,
                    f"display name '{record.from_display_name}' matches known "
                    f"contact <{hit['email_norm']}> but address is "
                    f"<{snap.email_norm}>",
                )

    # auth_fail
    if record.auth_dmarc == "fail":
        _fire(out, "auth_fail", 1.0, "DMARC failed for sending domain")
    elif record.auth_dkim == "fail" and record.auth_spf in ("fail", "softfail"):
        _fire(out, "auth_fail", 0.8, "DKIM and SPF both failed")
    elif record.auth_spf == "softfail":
        _fire(out, "auth_fail", 0.4, "SPF softfail for sending domain")

    # reply_to_divergence (tier >= 2; cold senders get cold_replyto instead)
    if tier >= 2 and record.reply_to_email_norm:
        rt_rd = reg_domain(address_domain(record.reply_to_email_norm))
        if rt_rd != sender_rd and record.reply_to_email_norm not in snap.replyto_addrs:
            _fire(
                out,
                "reply_to_divergence",
                1.0,
                f"replies redirect to <{record.reply_to_email_norm}>, never "
                f"seen from this sender in {snap.n_messages} messages",
            )

    # embedded_addr_mismatch
    if record.from_display_name:
        for emb in emails_in_text(record.from_display_name):
            emb_rd = reg_domain(address_domain(emb))
            if emb_rd and emb_rd != sender_rd:
                _fire(
                    out,
                    "embedded_addr_mismatch",
                    1.0,
                    f"display name shows '{emb}' but real sender is "
                    f"<{snap.email_norm}>",
                )
                break


# --------------------------------------------------------------- Group B ----


def _std(m2: float | None, n: int) -> float:
    if m2 is None or n < 2:
        return 0.0
    return math.sqrt(max(m2, 0.0) / (n - 1))


def _behavioral_features(
    conn: sqlite3.Connection,
    record: MessageRecord,
    snap: ProfileSnapshot,
    out: list[FeatureResult],
) -> None:
    n = snap.n_messages
    conf = R.confidence(n)

    # attachments: first-ever vs novel-type (mutually exclusive)
    if record.attachments:
        exts = sorted({a.extension or a.mime_type for a in record.attachments})
        shown = ", ".join(f".{e}" if "." not in e and "/" not in e else e for e in exts)
        if snap.n_with_attachments == 0:
            _fire(
                out,
                "first_attachment_ever",
                conf,
                f"first attachment of any kind ({shown}) in {n} messages "
                f"from this sender",
            )
        else:
            novel = [
                a
                for a in record.attachments
                if (a.extension, a.mime_type) not in snap.attachment_types
            ]
            if novel:
                novel_shown = ", ".join(
                    sorted(
                        {
                            f".{a.extension}" if a.extension else a.mime_type
                            for a in novel
                        }
                    )
                )
                _fire(
                    out,
                    "attachment_type_novelty",
                    conf,
                    f"first {novel_shown} from this sender in {n} messages",
                )

    # link_domain_novelty
    if record.link_domains and record.links_extracted:
        novel = sorted(set(record.link_domains) - snap.link_domains)
        if novel:
            frac = len(novel) / len(record.link_domains)
            _fire(
                out,
                "link_domain_novelty",
                frac * conf,
                f"links to {', '.join(novel[:3])}"
                f"{' (+more)' if len(novel) > 3 else ''}, never linked before "
                f"({len(snap.link_domains)} prior link domains)",
            )

    # send_hour_anomaly
    if record.sent_hour_local is not None:
        hist = snap.hour_histogram
        n_hist = sum(hist)
        if n_hist >= R.MIN_BASELINE_N:
            a = R.HOUR_SMOOTHING_ALPHA
            p = (hist[record.sent_hour_local] + a) / (n_hist + 24 * a)
            raw = max(0.0, 1.0 - 24.0 * p)
            _fire(
                out,
                "send_hour_anomaly",
                raw,
                f"sent at {record.sent_hour_local:02d}:00 sender-local; "
                f"{hist[record.sent_hour_local]} of {n_hist} prior messages "
                f"at this hour",
            )

    # link_density_anomaly (one-sided: only unusually many links)
    if snap.links_mean is not None and record.n_links > 0:
        std = max(_std(snap.links_m2, n), 1.0)
        z = (record.n_links - snap.links_mean) / std
        raw = (z - R.LINKS_Z_START) / (R.LINKS_Z_FULL - R.LINKS_Z_START)
        _fire(
            out,
            "link_density_anomaly",
            raw,
            f"{record.n_links} links; sender's typical is "
            f"{snap.links_mean:.1f} ± {std:.1f}",
        )

    # size_anomaly (two-sided on log size)
    if snap.log_size_mean is not None:
        std = max(_std(snap.log_size_m2, n), 0.35)
        z = abs(math.log(max(record.size_bytes, 1)) - snap.log_size_mean) / std
        raw = (z - R.SIZE_Z_START) / (R.SIZE_Z_FULL - R.SIZE_Z_START)
        _fire(
            out,
            "size_anomaly",
            raw,
            f"{_fmt_size(record.size_bytes)} message; sender's typical is "
            f"{_fmt_size(int(math.exp(snap.log_size_mean)))}",
        )

    # dormant_resurrection — only alongside other flags
    if out and snap.last_msg_at is not None:
        gap = record.sent_at - snap.last_msg_at
        if gap >= R.DORMANT_MIN_GAP_SECONDS:
            med = median_gap_seconds(conn, snap.sender_id, record.sent_at)
            if med and gap > R.DORMANT_GAP_MULTIPLIER * med:
                _fire(
                    out,
                    "dormant_resurrection",
                    1.0,
                    f"first message in {gap // 86400} days, combined with "
                    f"other anomalies",
                )


# --------------------------------------------------------------- Group C ----


def _cold_features(
    record: MessageRecord, snap: ProfileSnapshot, out: list[FeatureResult]
) -> None:
    # full strength on a first-ever message, half while baseline is thin
    scale = 1.0 if snap.n_messages == 0 else 0.5
    who = (
        "a never-seen sender"
        if snap.n_messages == 0
        else f"a barely-known sender ({snap.n_messages} prior messages)"
    )
    if record.attachments:
        _fire(out, "cold_attachment", scale, f"attachment from {who}")
    if record.n_links > 0:
        _fire(out, "cold_links", scale, f"{record.n_links} link(s) from {who}")
    if record.reply_to_email_norm:
        rt_rd = reg_domain(address_domain(record.reply_to_email_norm))
        if rt_rd != snap.reg_domain:
            _fire(
                out,
                "cold_replyto",
                scale,
                f"{who} redirects replies to <{record.reply_to_email_norm}>",
            )


# ------------------------------------------------------------------ main ----


def compute_features(
    conn: sqlite3.Connection,
    record: MessageRecord,
    snap: ProfileSnapshot,
    tier: int,
) -> list[FeatureResult]:
    out: list[FeatureResult] = []
    _identity_features(conn, record, snap, tier, out)
    if tier >= 2 and snap.n_messages >= R.MIN_BASELINE_N:
        _behavioral_features(conn, record, snap, out)
    elif tier <= 1 and snap.n_messages < R.MIN_BASELINE_N:
        _cold_features(record, snap, out)
    return out


def total_score(features: list[FeatureResult]) -> float:
    return min(R.MAX_SCORE, sum(f.contribution for f in features))


def score_message(
    conn: sqlite3.Connection,
    message_row_id: int,
    record: MessageRecord,
    snap: ProfileSnapshot,
    tier: int,
) -> float:
    features = compute_features(conn, record, snap, tier)
    score = total_score(features)
    conn.execute(
        """INSERT OR REPLACE INTO message_scores
             (message_id, engine_version, trust_tier_at_scoring, baseline_n,
              anomaly_score, scored_at)
           VALUES (?,?,?,?,?,?)""",
        (
            message_row_id,
            R.ENGINE_VERSION,
            tier,
            snap.n_messages,
            score,
            int(time.time()),
        ),
    )
    conn.execute(
        "DELETE FROM message_score_features WHERE message_id = ?",
        (message_row_id,),
    )
    for f in features:
        conn.execute(
            """INSERT INTO message_score_features
                 (message_id, feature, raw_value, weight, contribution,
                  explanation)
               VALUES (?,?,?,?,?,?)""",
            (message_row_id, f.name, f.raw, f.weight, f.contribution, f.explanation),
        )
    return score
