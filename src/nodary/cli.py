"""nodary CLI: add-account, set-secret, set-source, sync, rebuild, ui."""

from __future__ import annotations

import argparse
import getpass
import sys
import time

from .storage import db as storage_db
from .storage.keys import get_or_create_db_key, set_account_secret


def _open():
    return storage_db.connect(storage_db.default_db_path(), get_or_create_db_key())


def cmd_add_account(args) -> int:
    conn = _open()
    cur = conn.execute(
        "INSERT INTO accounts (email, imap_host, imap_port, auth_method, created_at)"
        " VALUES (?,?,?,?,?)",
        (args.email.lower(), args.host, args.port, args.auth, int(time.time())),
    )
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (?,?)",
        (account_id, args.email.lower()),
    )
    for alias in args.alias or []:
        conn.execute(
            "INSERT OR IGNORE INTO user_identities (account_id, email_norm)"
            " VALUES (?,?)",
            (account_id, alias.lower()),
        )
    conn.commit()

    prompt = (
        "OAuth2 access token (stored in OS keychain): "
        if args.auth == "oauth2"
        else "App password (stored in OS keychain): "
    )
    secret = getpass.getpass(prompt)
    set_account_secret(account_id, secret)
    print(f"account #{account_id} added: {args.email} @ {args.host}")
    if args.auth == "oauth2":
        print("note: refresh the token with `nodary set-secret` when it expires.")
    return 0


def cmd_set_secret(args) -> int:
    secret = getpass.getpass("New secret (stored in OS keychain): ")
    set_account_secret(args.account_id, secret)
    print("updated.")
    return 0


def cmd_sync(args) -> int:
    from .imap_sync import ImapTransport, sync_account
    from .pipeline import rebuild
    from .storage.keys import get_account_secret

    conn = _open()
    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    if not accounts:
        print("no accounts. run: nodary add-account", file=sys.stderr)
        return 1
    mail_store = None
    claimed_uuids: set[str] = set()
    for acct in accounts:
        if acct["auth_method"] == "mail_store":
            from .mail_store import MailStore, MailStoreTransport

            if mail_store is None:
                mail_store = MailStore()
            # primary address first; aliases only as fallback. Identities may
            # be shared across accounts, so an alias can point at a store
            # that belongs to a different account — each store UUID may be
            # claimed by at most one account per sync.
            identities = [acct["email"]] + [
                r["email_norm"]
                for r in conn.execute(
                    "SELECT email_norm FROM user_identities"
                    " WHERE account_id = ? AND email_norm != ?",
                    (acct["id"], acct["email"]),
                )
            ]
            uuid = next(
                (
                    u
                    for u in (mail_store.detect_account_uuid(i) for i in identities)
                    if u is not None and u not in claimed_uuids
                ),
                None,
            )
            if uuid is None:
                print(
                    f"{acct['email']}: no unclaimed account found in the local"
                    " Apple Mail store",
                    file=sys.stderr,
                )
                return 1
            claimed_uuids.add(uuid)
            transport = MailStoreTransport(mail_store, uuid)
            stats = sync_account(conn, transport, acct["id"])
            if transport.skipped_transient:
                print(
                    f"  warning: {transport.skipped_transient} indexed "
                    "message(s) are not yet on disk and will be retried",
                    file=sys.stderr,
                )
            if transport.skipped_permanent:
                print(
                    f"  warning: {transport.skipped_permanent} indexed "
                    "message(s) had a corrupt or unparseable .emlx and were "
                    "skipped",
                    file=sys.stderr,
                )
        else:
            secret = get_account_secret(acct["id"])
            if not secret:
                print(
                    f"no credential for {acct['email']}; run nodary set-secret",
                    file=sys.stderr,
                )
                return 1
            transport = ImapTransport(acct["imap_host"], acct["imap_port"])
            try:
                if acct["auth_method"] == "oauth2":
                    transport.login_oauth2(acct["email"], secret)
                else:
                    transport.login_password(acct["email"], secret)
                stats = sync_account(conn, transport, acct["id"])
            finally:
                transport.logout()
        print(f"{acct['email']}: {stats.new_messages} new messages")
        if stats.invalidated_folders:
            print(
                f"  UIDVALIDITY changed, refetched: "
                f"{', '.join(stats.invalidated_folders)}"
            )
        if stats.initial_backfill or stats.invalidated_folders:
            print("  replaying history for exact baselines…")
            n = rebuild(conn)
            print(f"  rebuilt profiles and scores from {n} messages")
    return 0


def cmd_set_source(args) -> int:
    conn = _open()
    row = conn.execute(
        "SELECT email FROM accounts WHERE id = ?", (args.account_id,)
    ).fetchone()
    if row is None:
        print(f"no account #{args.account_id}", file=sys.stderr)
        return 1
    method = "mail_store" if args.source == "mail-store" else args.auth
    conn.execute(
        "UPDATE accounts SET auth_method = ? WHERE id = ?",
        (method, args.account_id),
    )
    # different sources use different folder layouts and UID spaces; stale
    # facts would double-count history under new folder ids
    conn.execute(
        "DELETE FROM messages WHERE folder_id IN"
        " (SELECT id FROM folders WHERE account_id = ?)",
        (args.account_id,),
    )
    conn.execute(
        "DELETE FROM skipped_messages WHERE folder_id IN"
        " (SELECT id FROM folders WHERE account_id = ?)",
        (args.account_id,),
    )
    conn.execute(
        "UPDATE folders SET last_seen_uid = 0, uidvalidity = NULL WHERE account_id = ?",
        (args.account_id,),
    )
    conn.commit()
    print(f"account #{args.account_id} ({row['email']}) source -> {args.source}")
    print("  existing synced facts cleared; the next sync refetches from scratch")
    return 0


def cmd_rebuild(args) -> int:
    from .pipeline import rebuild

    conn = _open()
    n = rebuild(conn)
    print(f"rebuilt profiles, tiers, and scores from {n} messages")
    return 0


def cmd_status(args) -> int:
    from .scoring.registry import ENGINE_VERSION

    conn = _open()
    encryption = storage_db.get_meta(conn, "encryption") or "unknown"
    print(f"engine v{ENGINE_VERSION} | db encryption: {encryption}")
    accounts = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    if not accounts:
        print("no accounts. run: nodary add-account")
        return 1
    for acct in accounts:
        print(f"account #{acct['id']} {acct['email']} [{acct['auth_method']}]")
        rows = conn.execute(
            "SELECT name, role, uidvalidity, last_seen_uid, last_synced_at"
            " FROM folders WHERE account_id = ? ORDER BY id",
            (acct["id"],),
        ).fetchall()
        if not rows:
            print("  no folders synced yet")
        for f in rows:
            synced = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(f["last_synced_at"]))
                if f["last_synced_at"]
                else "never"
            )
            print(
                f"  {f['name']} ({f['role']}): uidvalidity={f['uidvalidity']},"
                f" last_seen_uid={f['last_seen_uid']}, last_synced={synced}"
            )
    n_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_in = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction = 'in'"
    ).fetchone()[0]
    n_senders = conn.execute("SELECT COUNT(*) FROM senders").fetchone()[0]
    print(f"messages: {n_msgs} ({n_in} incoming) | senders: {n_senders}")
    return 0


def cmd_calibrate(args) -> int:
    from .calibration import render_markdown, run_calibration

    print(render_markdown(run_calibration()), end="")
    return 0


def cmd_ui(args) -> int:
    from .ui import run

    run(_open(), port=args.port, tls=not args.no_tls)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="nodary",
        description="Local-first email heuristic analysis. All analysis "
        "on-device; nothing leaves this machine.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-account", help="register a read-only IMAP account")
    a.add_argument("email")
    a.add_argument("--host", required=True)
    a.add_argument("--port", type=int, default=993)
    a.add_argument("--auth", choices=["oauth2", "app_password"], default="app_password")
    a.add_argument(
        "--alias", action="append", help="additional address that is 'me' (repeatable)"
    )
    a.set_defaults(fn=cmd_add_account)

    s = sub.add_parser("set-secret", help="update an account's keychain secret")
    s.add_argument("account_id", type=int)
    s.set_defaults(fn=cmd_set_secret)

    y = sub.add_parser("sync", help="incremental read-only sync + scoring")
    y.set_defaults(fn=cmd_sync)

    c = sub.add_parser(
        "set-source",
        help="switch an account between IMAP and the local Apple Mail store",
    )
    c.add_argument("account_id", type=int)
    c.add_argument("source", choices=["imap", "mail-store"])
    c.add_argument(
        "--auth",
        choices=["oauth2", "app_password"],
        default="app_password",
        help="auth method restored when switching back to imap",
    )
    c.set_defaults(fn=cmd_set_source)

    r = sub.add_parser("rebuild", help="recompute all profiles/tiers/scores from facts")
    r.set_defaults(fn=cmd_rebuild)

    t = sub.add_parser(
        "status", help="show accounts, folder sync state, engine version, encryption"
    )
    t.set_defaults(fn=cmd_status)

    k = sub.add_parser(
        "calibrate",
        help="replay the bundled labeled corpus and report score distributions",
    )
    k.set_defaults(fn=cmd_calibrate)

    u = sub.add_parser("ui", help="serve the local dashboard (127.0.0.1)")
    u.add_argument("--port", type=int, default=8321)
    u.add_argument(
        "--no-tls",
        action="store_true",
        help="serve plain HTTP even if a local mkcert certificate is available",
    )
    u.set_defaults(fn=cmd_ui)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
