"""Local dashboard. Binds 127.0.0.1 only; serves no external assets and makes
no outbound requests — the page is a single self-contained HTML document.
Served over TLS with a locally-trusted mkcert certificate when available
(see tls.py); otherwise plain HTTP with a warning."""

from __future__ import annotations

import math
import sqlite3
import sys

from flask import Flask, jsonify, render_template, request

from ..feature_extraction.profiles import load_snapshot
from ..feature_extraction.records import HIST_HOURS, unpack_hist
from ..scoring.tiers import TIER_LABELS, matching_tier_rule
from ..storage import get_meta
from . import tls as _tls

# Shared projection for scored incoming rows. No subject, body, filename,
# or full URL columns exist on these tables; keep it that way.
_SCORED_MESSAGE_COLS = """m.id, m.message_id, m.sender_id, m.from_email_norm,
  m.from_display_name, m.sent_at,
  m.n_attachments, m.n_links, m.size_bytes,
  sc.anomaly_score,
  COALESCE(p.trust_tier, sc.trust_tier_at_scoring) AS tier,
  sc.trust_tier_at_scoring, sc.baseline_n, sc.engine_version"""


def _features(conn: sqlite3.Connection, message_id: int) -> list[dict]:
    return [
        dict(f)
        for f in conn.execute(
            """SELECT feature, raw_value, weight, contribution, explanation
               FROM message_score_features WHERE message_id = ?
               ORDER BY contribution DESC""",
            (message_id,),
        )
    ]


def _scored_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tier_label"] = TIER_LABELS[row["tier"]]
    d["features"] = _features(conn, row["id"])
    return d


def _welford_std(m2: float | None, n: int) -> float | None:
    if m2 is None or n < 2:
        return None
    return math.sqrt(max(m2, 0.0) / (n - 1))


def _sender_detail(conn: sqlite3.Connection, sender_id: int) -> dict | None:
    """Assemble the local-only sender drill-down from existing profile tables.

    Returns structural baselines only: no subjects, filenames, full URLs,
    or body text are selected.
    """
    row = conn.execute(
        """SELECT s.id, s.email_norm, s.domain, s.reg_domain, s.is_freemail,
                  s.first_seen_at, s.last_seen_at,
                  p.n_messages, p.n_threads, p.n_replied_threads,
                  p.n_user_initiated, p.trust_tier, p.hour_histogram,
                  p.log_size_mean, p.log_size_m2, p.links_mean, p.links_m2,
                  p.n_with_attachments, p.n_with_links, p.n_replyto_divergent,
                  p.first_msg_at, p.last_msg_at
           FROM senders s LEFT JOIN sender_profiles p ON p.sender_id = s.id
           WHERE s.id = ?""",
        (sender_id,),
    ).fetchone()
    if row is None:
        return None

    snap = load_snapshot(conn, sender_id)
    tier, rule = matching_tier_rule(conn, snap)
    n = row["n_messages"] or 0
    first = row["first_msg_at"]
    last = row["last_msg_at"]
    span = last - first if first and last and first > 0 and last > 0 else None
    hour_blob = row["hour_histogram"]
    hours = unpack_hist(hour_blob, HIST_HOURS) if hour_blob else [0] * HIST_HOURS
    typical_bytes = (
        int(round(math.exp(row["log_size_mean"])))
        if row["log_size_mean"] is not None
        else None
    )
    display = conn.execute(
        "SELECT name_norm FROM sender_display_names"
        " WHERE sender_id = ? ORDER BY n DESC LIMIT 1",
        (sender_id,),
    ).fetchone()
    attachment_types = [
        {"extension": r["extension"], "mime_type": r["mime_type"], "n": r["n"]}
        for r in conn.execute(
            "SELECT extension, mime_type, n FROM sender_attachment_types"
            " WHERE sender_id = ? ORDER BY n DESC, extension, mime_type",
            (sender_id,),
        )
    ]
    link_domains = [
        {"reg_domain": r["reg_domain"], "n": r["n"]}
        for r in conn.execute(
            "SELECT reg_domain, n FROM sender_link_domains"
            " WHERE sender_id = ? ORDER BY n DESC, reg_domain",
            (sender_id,),
        )
    ]
    reply_to = [
        {"email_norm": r["email_norm"], "n": r["n"]}
        for r in conn.execute(
            "SELECT email_norm, n FROM sender_replyto_addrs"
            " WHERE sender_id = ? ORDER BY n DESC, email_norm",
            (sender_id,),
        )
    ]
    recent = conn.execute(
        f"""SELECT {_SCORED_MESSAGE_COLS}
            FROM messages m
            JOIN message_scores sc ON sc.message_id = m.id
            LEFT JOIN sender_profiles p ON p.sender_id = m.sender_id
            WHERE m.direction = 'in' AND m.sender_id = ?
            ORDER BY m.sent_at DESC
            LIMIT 20""",
        (sender_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "email_norm": row["email_norm"],
        "display_name": display["name_norm"] if display else None,
        "domain": row["domain"],
        "reg_domain": row["reg_domain"],
        "is_freemail": bool(row["is_freemail"]),
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "trust_tier": tier,
        "tier_label": TIER_LABELS[tier],
        "tier_rule": rule,
        "n_messages": n,
        "n_threads": row["n_threads"] or 0,
        "n_replied_threads": row["n_replied_threads"] or 0,
        "n_user_initiated": row["n_user_initiated"] or 0,
        "n_with_attachments": row["n_with_attachments"] or 0,
        "n_with_links": row["n_with_links"] or 0,
        "n_replyto_divergent": row["n_replyto_divergent"] or 0,
        "first_msg_at": first,
        "last_msg_at": last,
        "span_seconds": span,
        "hour_histogram": hours,
        "size": {
            "typical_bytes": typical_bytes,
            "log_mean": row["log_size_mean"],
            "log_std": _welford_std(row["log_size_m2"], n),
        },
        "links": {
            "mean": row["links_mean"],
            "std": _welford_std(row["links_m2"], n),
            "n_with_links": row["n_with_links"] or 0,
        },
        "attachment_types": attachment_types,
        "link_domains": link_domains,
        "reply_to": reply_to,
        "recent_messages": [_scored_payload(conn, r) for r in recent],
    }


def create_app(conn: sqlite3.Connection) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        counts = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS n_in"
            " FROM messages"
        ).fetchone()
        return jsonify(
            {
                "messages": counts["n"],
                "incoming": counts["n_in"] or 0,
                "senders": conn.execute("SELECT COUNT(*) FROM senders").fetchone()[0],
                "encryption": get_meta(conn, "encryption"),
            }
        )

    @app.get("/api/messages")
    def messages():
        try:
            limit = max(1, min(int(request.args.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        tier = request.args.get("tier")
        where, params = "", []
        if tier is not None:
            try:
                params.append(int(tier))
            except ValueError:
                pass  # malformed tier filter: serve unfiltered
            else:
                where = "AND COALESCE(p.trust_tier, sc.trust_tier_at_scoring) = ?"
        rows = conn.execute(
            f"""SELECT {_SCORED_MESSAGE_COLS}
                FROM messages m
                JOIN message_scores sc ON sc.message_id = m.id
                LEFT JOIN sender_profiles p ON p.sender_id = m.sender_id
                WHERE m.direction = 'in' {where}
                ORDER BY sc.anomaly_score DESC, m.sent_at DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return jsonify([_scored_payload(conn, r) for r in rows])

    @app.get("/api/senders/<int:sender_id>")
    def sender(sender_id: int):
        detail = _sender_detail(conn, sender_id)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)

    return app


def run(conn: sqlite3.Connection, port: int = 8321, tls: bool = True) -> None:
    app = create_app(conn)
    context = None
    if tls:
        pair = _tls.ensure_certificate()
        if pair:
            context = (str(pair[0]), str(pair[1]))
    scheme = "https" if context else "http"
    print(f"nodary dashboard: {scheme}://127.0.0.1:{port}/  (local only)")
    if tls and context is None:
        print(
            "warning: serving plain HTTP — no local certificate found and\n"
            "  mkcert is not installed. For TLS: brew install mkcert &&"
            " mkcert -install\n"
            "  then restart the dashboard (certificate generation is fully"
            " local).",
            file=sys.stderr,
        )
    app.run(host="127.0.0.1", port=port, debug=False, ssl_context=context)
