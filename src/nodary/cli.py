"""nodary CLI: add-account, sync, rebuild, ui."""

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
    for acct in accounts:
        if acct["auth_method"] == "mail_store":
            from .mail_store import MailStore, MailStoreTransport

            if mail_store is None:
                mail_store = MailStore()
            uuid = mail_store.detect_account_uuid(acct["email"])
            if uuid is None:
                print(
                    f"{acct['email']}: not found in the local Apple Mail store",
                    file=sys.stderr,
                )
                return 1
            transport = MailStoreTransport(mail_store, uuid)
            stats = sync_account(conn, transport, acct["id"])
            if transport.skipped:
                print(
                    f"  warning: {transport.skipped} indexed message(s) had no "
                    "readable .emlx and were skipped",
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
    method = "mail_store" if args.source == "mail-store" else "app_password"
    conn.execute(
        "UPDATE accounts SET auth_method = ? WHERE id = ?",
        (method, args.account_id),
    )
    conn.commit()
    print(f"account #{args.account_id} ({row['email']}) source -> {args.source}")
    return 0


def cmd_rebuild(args) -> int:
    from .pipeline import rebuild

    conn = _open()
    n = rebuild(conn)
    print(f"rebuilt profiles, tiers, and scores from {n} messages")
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
    c.set_defaults(fn=cmd_set_source)

    r = sub.add_parser("rebuild", help="recompute all profiles/tiers/scores from facts")
    r.set_defaults(fn=cmd_rebuild)

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
