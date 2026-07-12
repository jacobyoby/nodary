"""Local dashboard. Binds 127.0.0.1 only; serves no external assets and makes
no outbound requests — the page is a single self-contained HTML document."""

from __future__ import annotations

import sqlite3

from flask import Flask, jsonify, render_template, request

from ..scoring.tiers import TIER_LABELS
from ..storage import get_meta


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
        limit = min(int(request.args.get("limit", 200)), 1000)
        tier = request.args.get("tier")
        where, params = "", []
        if tier is not None:
            where = "AND COALESCE(p.trust_tier, sc.trust_tier_at_scoring) = ?"
            params.append(int(tier))
        rows = conn.execute(
            f"""SELECT m.id, m.from_email_norm, m.from_display_name, m.sent_at,
                  m.n_attachments, m.n_links, m.size_bytes,
                  sc.anomaly_score,
                  COALESCE(p.trust_tier, sc.trust_tier_at_scoring) AS tier,
                  sc.trust_tier_at_scoring, sc.baseline_n, sc.engine_version
                FROM messages m
                JOIN message_scores sc ON sc.message_id = m.id
                LEFT JOIN sender_profiles p ON p.sender_id = m.sender_id
                WHERE m.direction = 'in' {where}
                ORDER BY sc.anomaly_score DESC, m.sent_at DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        out = []
        for r in rows:
            features = [
                dict(f)
                for f in conn.execute(
                    """SELECT feature, raw_value, weight, contribution, explanation
                       FROM message_score_features WHERE message_id = ?
                       ORDER BY contribution DESC""",
                    (r["id"],),
                )
            ]
            d = dict(r)
            d["tier_label"] = TIER_LABELS[r["tier"]]
            d["features"] = features
            out.append(d)
        return jsonify(out)

    @app.get("/api/senders/<int:sender_id>")
    def sender(sender_id: int):
        s = conn.execute(
            """SELECT s.*, p.n_messages, p.n_replied_threads, p.n_user_initiated,
                 p.trust_tier, p.n_with_attachments, p.n_with_links,
                 p.first_msg_at, p.last_msg_at
               FROM senders s LEFT JOIN sender_profiles p ON p.sender_id = s.id
               WHERE s.id = ?""",
            (sender_id,),
        ).fetchone()
        if s is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(dict(s))

    return app


def run(conn: sqlite3.Connection, port: int = 8321) -> None:
    app = create_app(conn)
    print(f"nodary dashboard: http://127.0.0.1:{port}/  (local only)")
    app.run(host="127.0.0.1", port=port, debug=False)
