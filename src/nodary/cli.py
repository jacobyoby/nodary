"""nodary CLI: add-account, set-secret, set-source, sync, rebuild, ui."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
import time
from pathlib import Path

from .storage import db as storage_db
from .storage.keys import (
    get_account_secret,
    get_or_create_db_key,
    set_account_secret,
    set_refresh_token,
)


def _open():
    return storage_db.connect(storage_db.default_db_path(), get_or_create_db_key())


def _account_error(conn, acct, reason: str) -> None:
    """Print a per-account failure and persist it for the dashboard.

    last_error is the persisted (not derived) string surfaced by /api/status.
    It is cleared on the next successful sync of that account.
    """
    print(f"account #{acct['id']} ({acct['email']}): {reason}", file=sys.stderr)
    conn.execute(
        "UPDATE accounts SET last_error = ? WHERE id = ?",
        (reason, acct["id"]),
    )
    conn.commit()


def _clear_account_error(conn, account_id: int) -> None:
    conn.execute("UPDATE accounts SET last_error = NULL WHERE id = ?", (account_id,))
    conn.commit()


def cmd_add_account(args) -> int:
    # Prompt first so Ctrl-C / EOF cannot leave a row with no keychain secret.
    prompt = (
        "OAuth2 access token (stored in OS keychain): "
        if args.auth == "oauth2"
        else "App password (stored in OS keychain): "
    )
    try:
        secret = getpass.getpass(prompt)
    except (KeyboardInterrupt, EOFError):
        print("aborted.", file=sys.stderr)
        return 1

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
    set_account_secret(account_id, secret)

    if args.auth == "oauth2":
        rt_prompt = "OAuth2 refresh token (optional; stored in OS keychain): "
        rt = getpass.getpass(rt_prompt)
        if rt:
            set_refresh_token(account_id, rt)
            print(
                f"account #{account_id} added: {args.email} @ {args.host}"
                " (refresh token stored; access token will auto-renew)"
            )
        else:
            print(f"account #{account_id} added: {args.email} @ {args.host}")
            print(
                "note: no refresh token stored; use"
                " `nodary set-secret <id> --refresh-token` to enable"
                " auto-renewal."
            )
    else:
        print(f"account #{account_id} added: {args.email} @ {args.host}")
    return 0


def cmd_set_secret(args) -> int:
    if args.refresh_token:
        rt = getpass.getpass("New refresh token (stored in OS keychain): ")
        set_refresh_token(args.account_id, rt)
        print("refresh token updated.")
        return 0
    secret = getpass.getpass("New secret (stored in OS keychain): ")
    set_account_secret(args.account_id, secret)
    print("updated.")
    return 0


class _AuthExpired(Exception):
    """Raised when an IMAP auth failure is detected (login or mid-session)."""


def _is_auth_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like an IMAP authentication failure."""
    text = str(exc).lower()
    return any(
        kw in text for kw in ("auth", "login", "invalid credentials", "authentication")
    )


def _oauth2_login_with_refresh(transport, acct, secret: str) -> None:
    """Attempt IMAP OAuth2 login; on auth failure, try token refresh first.

    Raises ``_AuthExpired`` if the refresh also fails or no refresh token is
    available.
    """
    from .auth import TokenRefreshError, detect_provider, refresh_access_token

    try:
        transport.login_oauth2(acct["email"], secret)
    except Exception as exc:
        if not _is_auth_error(exc):
            raise
        provider = detect_provider(acct["imap_host"])
        if provider is None:
            raise _AuthExpired(
                f"account #{acct['id']}: auth failed and provider is unknown"
                f" (host={acct['imap_host']}); cannot auto-refresh"
            ) from exc
        try:
            new_token = refresh_access_token(acct["id"], provider)
        except TokenRefreshError as rerr:
            raise _AuthExpired(str(rerr)) from exc
        # Retry login with the refreshed token.
        try:
            transport.login_oauth2(acct["email"], new_token)
        except Exception as retry_exc:
            raise _AuthExpired(
                f"account #{acct['id']}: login failed after token refresh: {retry_exc}"
            ) from retry_exc


def _try_refresh_and_relogin(transport, acct) -> bool:
    """Attempt to refresh the token and re-login on an existing transport.

    Returns True on success, False otherwise. On success the transport is
    re-logged-in and ready for another sync pass.
    """
    from .auth import TokenRefreshError, detect_provider, refresh_access_token

    provider = detect_provider(acct["imap_host"])
    if provider is None:
        return False
    try:
        new_token = refresh_access_token(acct["id"], provider)
    except TokenRefreshError:
        return False
    # Re-login: drop the old connection, create a fresh one.
    with contextlib.suppress(Exception):
        transport.logout()
    transport.client = transport._imapclient.IMAPClient(
        acct["imap_host"], port=acct["imap_port"], ssl=True
    )
    transport.login_oauth2(acct["email"], new_token)
    return True


def cmd_sync(args) -> int:
    from .imap_sync import ImapTransport, sync_account
    from .pipeline import rebuild

    sync_kwargs = {
        "batch_size": args.batch_size,
        "text_fetch_max_age_days": (
            None if args.text_fetch_age_days == 0 else args.text_fetch_age_days
        ),
    }

    conn = _open()
    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    if not accounts:
        print("no accounts. run: nodary add-account", file=sys.stderr)
        return 1
    mail_store = None
    claimed_uuids: set[str] = set()
    any_failed = False
    for acct in accounts:
        if acct["auth_method"] == "mail_store":
            from .mail_store import MailStore, MailStoreLayoutError, MailStoreTransport

            if mail_store is None:
                try:
                    mail_store = MailStore()
                except MailStoreLayoutError as exc:
                    print(str(exc), file=sys.stderr)
                    return 1
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
                _account_error(
                    conn,
                    acct,
                    "no unclaimed account found in the local Apple Mail store",
                )
                any_failed = True
                continue
            claimed_uuids.add(uuid)
            transport = MailStoreTransport(mail_store, uuid)
            stats = sync_account(conn, transport, acct["id"], **sync_kwargs)
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
                _account_error(conn, acct, "no credential; run nodary set-secret")
                any_failed = True
                continue
            transport = ImapTransport(acct["imap_host"], acct["imap_port"])
            try:
                if acct["auth_method"] == "oauth2":
                    _oauth2_login_with_refresh(transport, acct, secret)
                else:
                    transport.login_password(acct["email"], secret)
                stats = sync_account(conn, transport, acct["id"], **sync_kwargs)
            except Exception as exc:
                # Handle auth failures (login-time or mid-session) for
                # OAuth2 accounts. Non-auth or non-OAuth2 errors propagate.
                is_auth_expired = isinstance(exc, _AuthExpired)
                is_mid_session = (
                    acct["auth_method"] == "oauth2"
                    and not is_auth_expired
                    and _is_auth_error(exc)
                )
                if not is_auth_expired and not is_mid_session:
                    raise

                if is_mid_session:
                    # Token expired mid-session: refresh and retry once.
                    if _try_refresh_and_relogin(transport, acct):
                        stats = sync_account(conn, transport, acct["id"], **sync_kwargs)
                        # Success: fall through to normal output.
                    else:
                        _account_error(
                            conn,
                            acct,
                            f"auth expired mid-sync and refresh failed: {exc}",
                        )
                        any_failed = True
                        continue
                else:
                    # Login failed and refresh also failed.
                    _account_error(conn, acct, str(exc))
                    any_failed = True
                    continue
            finally:
                transport.logout()
        _clear_account_error(conn, acct["id"])
        print(f"{acct['email']}: {stats.new_messages} new messages")
        if stats.server_deleted:
            print(f"  {stats.server_deleted} message(s) marked server-deleted")
        if stats.invalidated_folders:
            print(
                f"  UIDVALIDITY changed, refetched: "
                f"{', '.join(stats.invalidated_folders)}"
            )
        if stats.initial_backfill or stats.invalidated_folders:
            print("  replaying history for exact baselines…")
            n = rebuild(conn)
            print(f"  rebuilt profiles and scores from {n} messages")
    return 1 if any_failed else 0


def cmd_set_source(args) -> int:
    conn = _open()
    row = conn.execute(
        "SELECT email FROM accounts WHERE id = ?", (args.account_id,)
    ).fetchone()
    if row is None:
        print(f"no account #{args.account_id}", file=sys.stderr)
        return 1
    # Validate the mail-store layout before committing the switch, so we
    # never clear facts for a source that cannot be read.
    if args.source == "mail-store":
        from .mail_store import MailStoreLayoutError, detect_mail_store_root

        try:
            detect_mail_store_root(configured_path=os.environ.get("NODARY_MAIL_STORE"))
        except MailStoreLayoutError as exc:
            print(str(exc), file=sys.stderr)
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
    from .storage.psl import get_current_psl_identity

    conn = _open()
    n = rebuild(conn)
    # Profiles were just rebuilt with the current PSL; record it so that
    # future tldextract upgrades are detected as drift rather than
    # silently shifting domain extraction.
    storage_db.set_meta(conn, "psl_version", get_current_psl_identity())
    conn.commit()
    print(f"rebuilt profiles, tiers, and scores from {n} messages")
    return 0


def cmd_status(args) -> int:
    from .scoring.registry import ENGINE_VERSION

    conn = _open()
    encryption = storage_db.get_meta(conn, "encryption") or "unknown"
    print(f"engine v{ENGINE_VERSION} | db encryption: {encryption}")

    # PSL version info
    psl = storage_db.get_psl_drift_info(conn)
    print(f"psl: {psl['current']} (stored: {psl['stored']})")
    if psl["drift"]:
        print(
            "  ⚠ PSL has changed since profiles were built."
            " Run `nodary rebuild` to update.",
            file=sys.stderr,
        )

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
    # Server-deleted: locally retained but no longer on the server.
    n_deleted = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted_upstream = 1"
    ).fetchone()[0]
    if n_deleted:
        print(f"server-deleted (retained for baselines): {n_deleted}")
        rows = conn.execute(
            "SELECT f.name, COUNT(*) AS cnt"
            " FROM messages m JOIN folders f ON f.id = m.folder_id"
            " WHERE m.deleted_upstream = 1"
            " GROUP BY f.name ORDER BY cnt DESC",
        ).fetchall()
        for r in rows:
            print(f"  {r['name']}: {r['cnt']}")
    return 0


def cmd_calibrate(args) -> int:
    from .calibration import render_markdown, run_calibration

    print(render_markdown(run_calibration()), end="")
    return 0


def cmd_export_profile(args) -> int:
    import hashlib
    import json
    import tarfile
    import tempfile
    from datetime import UTC, datetime

    from .scoring.registry import ENGINE_VERSION

    db_path = storage_db.default_db_path()
    if not db_path.exists():
        print(f"no database found at {db_path}", file=sys.stderr)
        return 1

    # Open DB to read metadata; checkpoint WAL so the file is self-contained.
    conn = _open()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    schema_version = storage_db.get_meta(conn, "schema_version") or "unknown"
    encryption_mode = storage_db.get_meta(conn, "encryption") or "none"
    identities = [
        r["email_norm"]
        for r in conn.execute(
            "SELECT DISTINCT email_norm FROM user_identities"
        ).fetchall()
    ]
    conn.close()

    # Read raw DB for hashing
    db_bytes = db_path.read_bytes()
    db_hash = hashlib.sha256(db_bytes).hexdigest()

    # Map internal mode to human-readable
    mode_label = "plain" if encryption_mode == "none" else encryption_mode

    manifest = {
        "schema_version": schema_version,
        "encryption_mode": mode_label,
        "engine_version": ENGINE_VERSION,
        "psl_identity": identities,
        "exported_at": datetime.now(UTC).isoformat(),
        "db_hash": db_hash,
    }

    output_path = args.output
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        manifest_path = tmppath / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        db_copy = tmppath / "nodary.db"
        db_copy.write_bytes(db_bytes)

        guidance_name = "keychain_guidance.txt"
        if args.include_secrets:
            guidance_text = (
                "Keychain Export Guidance\n"
                "========================\n\n"
                "This archive does NOT contain IMAP passwords or OAuth tokens.\n"
                "Those secrets live only in the OS keychain and must be\n"
                "re-entered on the target machine.\n\n"
                "After importing on the target machine:\n"
                "  1. Run: nodary set-secret <account_id>\n"
                "     for each account to re-enter the IMAP credential.\n"
                "  2. The SQLCipher database key is managed by the OS\n"
                "     keychain. A new key will be generated automatically\n"
                "     on first use if one does not exist.\n"
                "  3. If the imported database uses SQLCipher, set\n"
                "     NODARY_DB_KEY to the source machine's key (hex)\n"
                "     or re-export without encryption.\n"
            )
            guidance_path = tmppath / guidance_name
            guidance_path.write_text(guidance_text)

        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.json")
            tar.add(db_copy, arcname="nodary.db")
            if args.include_secrets:
                tar.add(guidance_path, arcname=guidance_name)

    print(f"exported profile to {output_path}")
    print(f"  encryption_mode: {mode_label}")
    print(f"  db_hash: {db_hash[:16]}…")
    print(f"  identities: {', '.join(identities) if identities else 'none'}")
    print()
    print("⚠  this archive is as sensitive as the live database.")
    print("   it contains all sender profiles, scores, and")
    print("   communication metadata. transfer securely and")
    print("   delete after import.")

    return 0


def cmd_import_profile(args) -> int:
    import hashlib
    import json
    import tarfile
    import tempfile

    input_path = Path(args.input)
    target_path = Path(args.target_db)

    if not input_path.exists():
        print(f"archive not found: {input_path}", file=sys.stderr)
        return 1

    if target_path.exists() and not args.force:
        print(f"target database already exists: {target_path}", file=sys.stderr)
        print("use --force to overwrite", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with tarfile.open(input_path, "r:gz") as tar:
                tar.extractall(tmpdir, filter="data")
        except (tarfile.TarError, Exception) as exc:
            print(f"invalid archive: {exc}", file=sys.stderr)
            return 1

        manifest_file = Path(tmpdir) / "manifest.json"
        db_file = Path(tmpdir) / "nodary.db"

        if not manifest_file.exists():
            print("invalid archive: missing manifest.json", file=sys.stderr)
            return 1
        if not db_file.exists():
            print("invalid archive: missing nodary.db", file=sys.stderr)
            return 1

        try:
            manifest = json.loads(manifest_file.read_text())
        except json.JSONDecodeError as exc:
            print(f"invalid manifest: {exc}", file=sys.stderr)
            return 1

        for field in ("schema_version", "encryption_mode", "engine_version", "db_hash"):
            if field not in manifest:
                print(f"invalid manifest: missing '{field}'", file=sys.stderr)
                return 1

        # Verify integrity
        db_bytes = db_file.read_bytes()
        actual_hash = hashlib.sha256(db_bytes).hexdigest()
        if actual_hash != manifest["db_hash"]:
            print("hash mismatch: archive may be corrupted", file=sys.stderr)
            print(f"  expected: {manifest['db_hash']}", file=sys.stderr)
            print(f"  actual:   {actual_hash}", file=sys.stderr)
            return 1

        encryption_mode = manifest["encryption_mode"]

        # Encryption mode mismatch checks
        if encryption_mode == "sqlcipher" and not storage_db.HAVE_SQLCIPHER:
            print(
                "error: archive was created with SQLCipher encryption but "
                "this installation does not have sqlcipher3 installed.",
                file=sys.stderr,
            )
            print(
                "install with: uv sync --extra sqlcipher",
                file=sys.stderr,
            )
            return 1

        if encryption_mode == "plain" and storage_db.HAVE_SQLCIPHER:
            print(
                "error: archive contains a plain-text database but this "
                "installation expects SQLCipher encryption.",
                file=sys.stderr,
            )
            print(
                "re-export from the source with sqlcipher installed, or "
                "import on a machine without the sqlcipher extra.",
                file=sys.stderr,
            )
            return 1

        # Validate SQLCipher key if encrypted
        if encryption_mode == "sqlcipher":
            try:
                key = get_or_create_db_key()
            except Exception as exc:
                print(f"cannot retrieve database key: {exc}", file=sys.stderr)
                return 1
            # Validate the key works on the imported DB
            try:
                test_conn = storage_db.connect(db_file, key)
                test_conn.close()
            except Exception:
                print(
                    "error: cannot open the imported SQLCipher database "
                    "with the available key.",
                    file=sys.stderr,
                )
                print(
                    "set NODARY_DB_KEY to the source machine's database key "
                    "(64 hex chars) and try again.",
                    file=sys.stderr,
                )
                return 1

        # Restore DB to target path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(db_bytes)
        with contextlib.suppress(OSError):
            os.chmod(target_path, 0o600)

    identities = manifest.get("psl_identity", [])
    print(f"imported profile to {target_path}")
    print(f"  schema_version: {manifest['schema_version']}")
    print(f"  encryption_mode: {encryption_mode}")
    print(f"  engine_version: {manifest['engine_version']}")
    if identities:
        print(f"  identities: {', '.join(identities)}")
    print()
    default = storage_db.default_db_path()
    print("to use this database:")
    print(f"  export NODARY_DB={target_path}")
    if str(target_path) != str(default):
        print(f"  # or copy to {default}")
    print()
    print("re-add IMAP credentials for each account:")
    print("  nodary set-secret <account_id>")

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
    s.add_argument(
        "--refresh-token",
        action="store_true",
        help="update the OAuth2 refresh token instead of the access"
        " token / app password",
    )
    s.set_defaults(fn=cmd_set_secret)

    y = sub.add_parser("sync", help="incremental read-only sync + scoring")
    y.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="number of UIDs to fetch per batch (default: 200)",
    )
    y.add_argument(
        "--text-fetch-age-days",
        type=int,
        default=90,
        help="skip text-part fetch for messages older than N days"
        " (default: 90; use 0 to fetch all)",
    )
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

    ep = sub.add_parser(
        "export-profile",
        help="export the profile database as an archive for machine migration",
    )
    ep.add_argument("--output", required=True, help="output archive path (.tar.gz)")
    ep.add_argument(
        "--include-secrets",
        action="store_true",
        help="include keychain export guidance (secrets are never included)",
    )
    ep.set_defaults(fn=cmd_export_profile)

    ip = sub.add_parser(
        "import-profile",
        help="import a profile database archive from machine migration",
    )
    ip.add_argument("--input", required=True, help="input archive path (.tar.gz)")
    ip.add_argument(
        "--target-db", required=True, help="target database path to restore to"
    )
    ip.add_argument(
        "--force", action="store_true", help="overwrite an existing database"
    )
    ip.set_defaults(fn=cmd_import_profile)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
