"""Shared fixtures: an in-memory database, an account, and a synthetic
mailbox builder used to construct sender histories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from nodary.feature_extraction.extract import record_from_message
from nodary.pipeline import ingest_message
from nodary.storage import connect

ME = "jacob@myco.com"
TZ = timezone(timedelta(hours=-5))
T0 = datetime(2026, 1, 5, 9, 30, tzinfo=TZ)  # a Monday morning


@pytest.fixture
def conn():
    c = connect(":memory:")
    c.execute(
        "INSERT INTO accounts (id, email, imap_host, auth_method, created_at)"
        " VALUES (1, ?, 'imap.test', 'app_password', 0)",
        (ME,),
    )
    c.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (1, ?)", (ME,)
    )
    c.execute(
        "INSERT INTO folders (id, account_id, name, role) VALUES"
        " (1, 1, 'INBOX', 'inbox'), (2, 1, 'Sent', 'sent')"
    )
    c.commit()
    return c


def make_email(
    from_addr: str,
    *,
    display: str | None = None,
    to: str = ME,
    when: datetime = T0,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    body: str = "hi, quick question about the quarterly numbers.",
    html: str | None = None,
    attachments: list[tuple[str, str, bytes]] = (),  # (filename, mime, data)
    reply_to: str | None = None,
    auth_results: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{display} <{from_addr}>" if display else from_addr
    msg["To"] = to
    msg["Subject"] = "synthetic"
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = message_id or f"<{abs(hash((from_addr, when))):x}@test>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    if reply_to:
        msg["Reply-To"] = reply_to
    if auth_results:
        msg["Authentication-Results"] = auth_results
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


class Mailbox:
    """Ingests synthetic messages through the real pipeline."""

    INBOX, SENT = 1, 2

    def __init__(self, conn):
        self.conn = conn
        self._uid = {self.INBOX: 0, self.SENT: 0}

    def _next(self, folder: int) -> int:
        self._uid[folder] += 1
        return self._uid[folder]

    def deliver(self, msg: EmailMessage) -> int:
        """Incoming message -> row id (scored on ingest)."""
        rec = record_from_message(msg, direction="in", my_addrs=frozenset({ME}))
        return ingest_message(self.conn, self.INBOX, self._next(self.INBOX), rec)

    def send(self, msg: EmailMessage) -> int:
        """Outgoing message (appears in Sent)."""
        rec = record_from_message(msg, direction="out", my_addrs=frozenset({ME}))
        return ingest_message(self.conn, self.SENT, self._next(self.SENT), rec)

    def reply_to(
        self, peer: str, original: EmailMessage, when: datetime | None = None
    ) -> int:
        when = when or T0
        reply = make_email(
            ME,
            to=peer,
            when=when,
            in_reply_to=original["Message-ID"],
            references=[original["Message-ID"]],
            body="thanks, will do.",
        )
        return self.send(reply)

    def establish_contact(
        self,
        peer: str,
        *,
        display: str | None = None,
        n: int = 20,
        start: datetime = T0,
        every_days: int = 3,
        body: str = "status update as usual, numbers attached below inline.",
        reply: bool = True,
    ) -> list[EmailMessage]:
        """Build a Tier-3 baseline: n incoming messages at consistent
        morning hours, one outgoing reply."""
        sent = []
        for i in range(n):
            m = make_email(
                peer,
                display=display,
                when=start + timedelta(days=i * every_days, minutes=7 * i % 90),
                body=body,
            )
            self.deliver(m)
            sent.append(m)
        if reply and sent:
            # reply after the last incoming message so ingestion order matches
            # sent_at order (as real incremental sync sees it)
            self.reply_to(
                peer, sent[-1], when=start + timedelta(days=n * every_days, hours=3)
            )
        return sent

    def score_of(self, msg_row_id: int) -> float:
        return self.conn.execute(
            "SELECT anomaly_score FROM message_scores WHERE message_id = ?",
            (msg_row_id,),
        ).fetchone()["anomaly_score"]

    def features_of(self, msg_row_id: int) -> dict[str, dict]:
        return {
            r["feature"]: dict(r)
            for r in self.conn.execute(
                "SELECT * FROM message_score_features WHERE message_id = ?",
                (msg_row_id,),
            )
        }

    def tier_of(self, email_norm: str) -> int:
        row = self.conn.execute(
            """SELECT p.trust_tier FROM sender_profiles p
               JOIN senders s ON s.id = p.sender_id WHERE s.email_norm = ?""",
            (email_norm,),
        ).fetchone()
        return row["trust_tier"] if row else 0


@pytest.fixture
def mailbox(conn):
    return Mailbox(conn)
