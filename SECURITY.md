# Security and privacy

Nodary is a local-first review tool. There is no hosted service, no
cross-install sync, and no telemetry. This file is the threat-model stub for
the privacy invariants; it is not a disclosure program for a remote product.

The behavioral profile is sensitive personally identifiable information. It
reveals correspondents, relationships, routines, and communication patterns.
It must never leave the machine.

## What Nodary is for

Scores are a local aid for reviewing:

- Lookalike-domain phishing that resembles an organization the user trusts.
- Display-name impersonation of a trusted contact from a different address.
- A compromised contact whose messages shift away from their established
  behavior (new payload type, link destination, sending hour, or shape).
- Cold outreach carrying links, attachments, or a redirecting Reply-To.

Familiarity is not treated as proof of safety. Identity features apply at
every trust tier. Scoring is deterministic and explainable; it is not a
verdict that a message is malicious.

## What Nodary will not do

- Telemetry of any kind, including "anonymous" usage stats.
- Cloud scoring APIs, shared reputation feeds, or optional OSINT lookups.
- Send, move, delete, report, or otherwise mutate mailbox contents.
- Bind the dashboard off `127.0.0.1`, or load external dashboard assets.
- Publish red-team or exploit writeups against third-party mail providers.

The only network connection the codebase may open is the read-only IMAP
session (plus Flask bound to localhost). Public Suffix List data, the
freemail list, and confusables are vendored; they are not fetched at
runtime.

## Privacy invariants

These are product rules, not aspirational docs. A change that needs new
persisted content must derive a structural value (count, extension,
registrable domain, histogram bucket) and document it in `docs/DESIGN.md`.

| Must not persist | Allowed residue |
|---|---|
| Subject lines | — |
| Body text (plain or HTML) | HTTP(S) link hostnames, reduced to registrable domains and counts |
| Attachment filenames and bytes | MIME type, derived extension, size |
| Full URLs (path, query, fragment) | `reg_domain` on `message_link_domains` / `sender_link_domains` |
| Incoming recipient lists | Count; outgoing `message_recipients` links a sent message to known contacts so Tier 3 is recomputable |
| IMAP passwords, OAuth tokens, DB key | OS keychain (`nodary/account/<id>`, `nodary/db-key`); `NODARY_DB_KEY` is a test/CI override |

Mail-store sync may record `skipped_messages.path` and a short reason for a
permanently unparseable `.emlx`. Message bytes are not stored.

Dashboard JSON (`/api/messages`, `/api/senders/<id>`, `/api/status`) is a
projection of the same tables and must omit the same fields.

## Credentials and encryption

Account secrets are written only through `set_account_secret` / the OS
keyring. The `accounts` table has no secret columns. With the `sqlcipher`
extra, the database is encrypted at rest with a 256-bit key held in the
keychain; without it, Nodary falls back to plain SQLite and records
`schema_meta.encryption = none`.

## Regression tests

`tests/test_privacy_invariants.py` plants unique subject, body, filename,
URL-path, and credential markers in a synthetic IMAP sync (and in
`nodary add-account`), then asserts they are absent from:

- every user-table column name and every SQL text cell
- `/api/messages`, `/api/senders/<id>`, and `/api/status` JSON
- on-disk database file bytes (including WAL)

The existing CI `test` job runs the suite with no network. Related
spot-checks live in `tests/test_sync_incremental.py`, `tests/test_normalize.py`,
and `tests/test_ui.py`.

## Reporting

This is an offline local app. If Nodary persists forbidden content, writes
credentials into the database, or opens outbound traffic beyond read-only
IMAP, open a GitHub issue against this repository.
