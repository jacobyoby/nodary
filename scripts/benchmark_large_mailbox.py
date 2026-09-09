#!/usr/bin/env python3
"""Benchmark large-mailbox backfill performance.

Synthesizes ~100k synthetic messages in a temporary SQLite DB (no real IMAP
needed), runs the sync pipeline, and measures wall time, peak RSS, and DB
file size.

Usage:
    python scripts/benchmark_large_mailbox.py [--messages N] [--batch-size N]
"""

from __future__ import annotations

import argparse
import resource
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

# Add src to path so we can import nodary
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nodary.imap_sync.client import FetchMeta
from nodary.imap_sync.sync import BATCH_SIZE, TEXT_FETCH_MAX_AGE_DAYS, sync_account
from nodary.storage import connect
from tests.fake_imap import _bodystructure, _sections


def make_synthetic_message(
    from_addr: str,
    when: datetime,
    uid: int,
    *,
    body: str = "Benchmark synthetic message body with a link: https://example.com/path",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bench@nodary.local"
    msg["Subject"] = f"bench-{uid}"
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = f"<bench-{uid}@nodary.local>"
    msg["Authentication-Results"] = "mx.nodary.local; spf=pass; dmarc=pass"
    msg.set_content(body)
    return msg


class BenchTransport:
    """Minimal transport that serves pre-built messages from memory."""

    def __init__(self, messages: dict[int, EmailMessage]):
        self._messages = messages
        self._selected: dict[int, EmailMessage] | None = None
        self.meta_fetches = 0
        self.part_fetches = 0

    def list_sync_folders(self):
        return [("INBOX", "inbox")]

    def select_readonly(self, name: str):
        self._selected = self._messages
        return {"uidvalidity": 1, "uidnext": max(self._messages) + 1}

    def new_uids(self, after_uid: int):
        return sorted(u for u in self._selected if u > after_uid)

    def fetch_meta(self, uids):
        self.meta_fetches += len(uids)
        out = {}
        for uid in uids:
            msg = self._selected.get(uid)
            if msg is None:
                continue
            raw = msg.as_bytes()
            out[uid] = {
                "header": raw,
                "size": len(raw),
                "bodystructure": _bodystructure(msg),
            }
        return FetchMeta(messages=out)

    def fetch_part(self, uid: int, section: str) -> bytes:
        self.part_fetches += 1
        msg = self._selected.get(uid)
        if msg is None:
            return b""
        return _sections(msg).get(section, b"")


def run_benchmark(
    n_messages: int = 100_000,
    batch_size: int = BATCH_SIZE,
    text_fetch_max_age_days: int | None = TEXT_FETCH_MAX_AGE_DAYS,
) -> dict:
    """Run the benchmark and return results."""
    print(f"Synthesizing {n_messages} messages...")
    t0 = time.monotonic()

    # Spread messages over 2 years, with varied senders
    now = datetime.now(UTC)
    senders = [f"sender{i}@domain{i % 50}.example" for i in range(200)]
    messages = {}
    for uid in range(1, n_messages + 1):
        # Spread evenly over 730 days
        days_ago = int((uid / n_messages) * 730)
        when = now - timedelta(days=days_ago, hours=uid % 24, minutes=uid % 60)
        sender = senders[uid % len(senders)]
        messages[uid] = make_synthetic_message(sender, when, uid)

    synth_time = time.monotonic() - t0
    print(f"  synthesized in {synth_time:.1f}s")

    transport = BenchTransport(messages)

    # Use a temp file for the DB so we can measure file size
    tmpdir = tempfile.mkdtemp(prefix="nodary-bench-")
    db_path = Path(tmpdir) / "bench.db"

    print(
        f"Running sync ({n_messages} messages, batch_size={batch_size},"
        f" text_fetch_max_age_days={text_fetch_max_age_days})..."
    )

    conn = connect(str(db_path))
    conn.execute(
        "INSERT INTO accounts (id, email, imap_host, auth_method, created_at)"
        " VALUES (1, 'bench@nodary.local', 'imap.test', 'app_password', 0)"
    )
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm)"
        " VALUES (1, 'bench@nodary.local')"
    )
    conn.commit()

    sync_start = time.monotonic()
    stats = sync_account(
        conn,
        transport,
        account_id=1,
        batch_size=batch_size,
        text_fetch_max_age_days=text_fetch_max_age_days,
    )
    sync_elapsed = time.monotonic() - sync_start

    # Peak RSS
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On macOS ru_maxrss is in bytes; on Linux in KB
    if sys.platform == "darwin":
        peak_rss_mb = peak_rss_kb / (1024 * 1024)
    else:
        peak_rss_mb = peak_rss_kb / 1024

    # DB file size
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    db_size_mb = db_path.stat().st_size / (1024 * 1024)

    results = {
        "n_messages": n_messages,
        "batch_size": batch_size,
        "text_fetch_max_age_days": text_fetch_max_age_days,
        "new_messages": stats.new_messages,
        "wall_time_s": round(sync_elapsed, 2),
        "peak_rss_mb": round(peak_rss_mb, 1),
        "db_size_mb": round(db_size_mb, 1),
        "meta_fetches": transport.meta_fetches,
        "part_fetches": transport.part_fetches,
        "messages_per_second": round(n_messages / sync_elapsed, 0),
    }

    # Cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark nodary large-mailbox backfill performance"
    )
    parser.add_argument(
        "--messages",
        type=int,
        default=100_000,
        help="number of synthetic messages (default: 100000)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"batch size for sync (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--text-fetch-age-days",
        type=int,
        default=TEXT_FETCH_MAX_AGE_DAYS,
        help=f"text-part fetch age gate in days (default: {TEXT_FETCH_MAX_AGE_DAYS})",
    )
    args = parser.parse_args()

    results = run_benchmark(
        n_messages=args.messages,
        batch_size=args.batch_size,
        text_fetch_max_age_days=args.text_fetch_age_days,
    )

    print("\n=== Benchmark Results ===")
    print(f"  Messages:              {results['n_messages']:,}")
    print(f"  Batch size:            {results['batch_size']}")
    print(f"  Text fetch age gate:   {results['text_fetch_max_age_days']} days")
    print(f"  New messages synced:   {results['new_messages']:,}")
    print(f"  Wall time:             {results['wall_time_s']:.1f}s")
    print(f"  Peak RSS:              {results['peak_rss_mb']:.0f} MB")
    print(f"  DB file size:          {results['db_size_mb']:.1f} MB")
    print(f"  Meta fetches:          {results['meta_fetches']:,}")
    print(f"  Part fetches:          {results['part_fetches']:,}")
    print(f"  Throughput:            {results['messages_per_second']:.0f} msg/s")


if __name__ == "__main__":
    main()
