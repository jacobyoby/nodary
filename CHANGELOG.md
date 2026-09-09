# Changelog

All notable changes to nodary are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The scoring engine has its own version
(`ENGINE_VERSION` in `src/nodary/scoring/registry.py`) recorded on every
stored score; bump it whenever a feature, weight, or threshold changes.

## [Unreleased]

### Added
- README donate link for Jacobrakai Foundation
  (`donate.stripe.com`, label: Donate / Support Jacobrakai Foundation —
  JACOBRAKAI FOUNDATION 501(c)(3)).
- README notes Jacobrakai Foundation 501(c)(3) status (EIN 33-3382083) and
  IRS Letter 947 dated September 3, 2026.
- Privacy-invariant regression tests (`tests/test_privacy_invariants.py`) and
  a `SECURITY.md` threat-model stub. After a synthetic sync with rich
  headers, body, and attachments, CI asserts the database, `/api/messages`
  JSON, and on-disk DB bytes omit subjects, body text, filenames, full URLs,
  and credentials.
- Dashboard sender drill-down: from a scored message, open a local-only sender
  view with the matching trust-tier rule, message counts and span, send-hour
  histogram, size/link-density baselines, known attachment types, known link
  domains, known Reply-To set, and recent messages with feature chips.
  `GET /api/senders/<id>` returns those baselines from existing profile
  tables; `GET /api/messages` now includes `sender_id`. No new persisted
  content; payloads still omit subjects, filenames, full URLs, and body text.

## [0.3.1] — 2026-09-08

### Added
- MIT license, CI/license badges, and GitHub topics; `license` metadata in
  `pyproject.toml`.
- `nodary set-source <id> imap --auth oauth2` restores the right auth method
  for OAuth2 accounts (previously always reverted to `app_password`).
- Automatic in-place migration widens the `accounts.auth_method` CHECK on
  databases created before `mail_store` existed.
- README documents all environment variables.
- `nodary status` command showing per-account folders with high-water marks,
  last sync time, engine version, and database encryption state.
- Dependabot weekly updates for the `uv` lockfile and GitHub Actions.

### Changed
- Locked `cryptography` 50.0.0 (from 49.0.0) for CVE-2026-69247. PKCS#7
  `encryptedKey` unwrap no longer leaks distinguishable errors or timing that
  could act as a Bleichenbacher oracle. Lockfile-only; not a direct
  dependency.

### Fixed
- `nodary sync` no longer exits at the first misconfigured account: each
  failure is printed to stderr with the account id and reason, remaining
  accounts still sync, and the process exits non-zero if any account failed.
  `add-account` now prompts for the secret before inserting the row, so
  Ctrl-C at the prompt cannot leave an account with no credential.
- Mail-store sync no longer stalls a folder behind a corrupt or unparseable
  `.emlx`: permanent parse failures are recorded in `skipped_messages` and the
  high-water mark advances past them, while absent files and failed
  `.partial.emlx` downloads still stop at the gap and retry. `nodary sync`
  reports the two skip counts separately.
- A base64 text part with invalid padding is recorded as
  `links_extracted=0` instead of an empty, fully scanned body. The previous
  catch-all returned empty bytes and let scoring treat the message as
  link-free, which suppressed anomalies and polluted sender baselines.
- Duplicate suppression: a message whose Message-ID, size, and date match an
  already-synced message is no longer ingested again. This drops
  double-delivered copies and the same delivery reaching two synced accounts
  (forwarding), which previously inflated sender baselines and cluttered the
  dashboard.
- **Security:** self-From alone no longer bypasses scoring. A message using
  the user's own address is classified incoming and scored unless it sits in
  the Sent folder or a receiving server stamped Authentication-Results
  without a DMARC failure (the shape of a genuine self-sent copy).
  Previously only a DMARC failure flipped the classification, so a self-From
  spoof relayed by a server that stamps no verdict was never scored.
- The sync high-water mark no longer advances past messages the transport
  could not serve (e.g. an `.emlx` not yet downloaded by Mail); they are
  retried on the next sync instead of being skipped forever.
- `set-source` clears the account's synced facts when switching transports,
  preventing double-counted history under the new folder layout.
- Mail-store account detection tries every registered identity, not only the
  primary address — primary first, and a store account can be claimed by at
  most one nodary account per sync (identities shared across accounts
  previously let a second account re-ingest the first account's store).
- The mail-store transport no longer decodes or retains attachment bytes when
  building message structure; only bounded text parts are held.
- Empty folders no longer mark every sync as an initial backfill (which
  triggered a full rebuild on each run).
- Tier 3 is labeled "established" (it includes user-initiated threads that
  have no reply yet, so "two-way" overstated it); stale design-doc and
  docstring claims corrected; `__version__` synced to 0.3.0.
- Messages with a missing or unparsable `Date` header no longer seed sender,
  domain, and tier timelines with the 1970 epoch: `sent_at=0` is kept as the
  unknown mark and excluded from first/last-seen, the Tier-2 span, median
  gaps, and dormant-resurrection math. A `Date` without a UTC offset is read
  as UTC instead of machine-local time, keeping `sent_at` and sender-local
  hour baselines deterministic.

## [0.3.0] — 2026-07-23

### Added
- CI (GitHub Actions): ruff lint, `ruff format --check`, and the test suite on
  Python 3.12 and 3.13, plus a separate job covering the `sqlcipher` extra so a
  missing wheel for one interpreter cannot red the whole matrix. `uv sync
  --locked` fails the build if `uv.lock` drifts from `pyproject.toml`, so a
  dependency edit cannot land without the lockfile.
- Apple Mail store transport (`nodary.mail_store`): sync accounts from
  `~/Library/Mail` (Envelope Index + `.emlx`) when direct IMAP is unavailable,
  e.g. Gmail under Google Advanced Protection. Strictly read-only; requires
  Full Disk Access. New CLI: `nodary set-source <id> imap|mail-store`.
- `accounts.auth_method` now allows `mail_store` (existing databases need a
  one-off table rebuild; new databases pick it up from the schema).

### Fixed
- SQLCipher connections crashed on first query: `sqlite3.Row` rejects
  sqlcipher3 cursors. Each dbapi module now supplies its own Row type.
- A malformed `.emlx` or unparseable message no longer aborts a mail-store
  sync; the message is skipped, counted, and reported after the sync.
- Direction and to-me detection compare normalized addresses on both sides;
  the previous raw-substring match never recognized identities whose stored
  form differs from the header (e.g. dotted gmail addresses).
- Mail-store `uidvalidity` derives from the mailbox ROWID instead of a
  constant, so a recreated Envelope Index (new machine, Mail reset) triggers
  invalidate-and-refetch instead of silently skipping mail whose rowids
  restarted below the high-water mark.

## [0.2.0] — 2026-07-21

### Added
- Dashboard TLS: `nodary ui` now serves HTTPS using a locally-trusted
  certificate generated by mkcert (`~/.nodary/tls/`, override with
  `NODARY_CERT_DIR`). Certificate generation is fully local — no ACME, no
  Certificate Transparency log entries (Let's Encrypt cannot issue for
  127.0.0.1 and would publish the hostname; deliberately not used). Falls
  back to plain HTTP on 127.0.0.1 with a visible warning when mkcert is
  absent; `--no-tls` forces plain HTTP.
- Dashboard test coverage: API endpoints, tier filtering, TLS wiring, and
  certificate resolution (mkcert stubbed; no network in tests).

### Changed
- Dashboard redesign: readable typography (system UI font, monospace kept
  for numerics), light mode via `prefers-color-scheme`, sender initial
  avatars with deterministic local colors, human-readable dates
  (today/yesterday/absolute), score shown as a severity-colored progress
  ring, per-feature contribution bars in the score decomposition. Still a
  single self-contained page with zero external assets and no outbound
  requests; tier badges, filters, and the "open in Apple Mail" deep link
  unchanged in behavior.

## [0.1.0] — 2026-07-12

### Added
- Read-only incremental IMAP sync (UIDVALIDITY/high-water-mark aware);
  headers + BODYSTRUCTURE only, bounded text-part fetch for link extraction.
- Local contact graph: per-sender profiles (frequency, recency, reply rate,
  send-hour/dow histograms, size and link-density running stats, attachment
  types, link domains, Reply-To addresses, display names).
- Trust tiers 0–3 computed from correspondence history; freemail domains
  never propagate Tier 1.
- Deterministic, explainable scoring engine (engine version 1.0.0):
  15 features in three groups (identity/spoofing, behavioral-shift vs own
  baseline, cold-contact context), each with named weight and explanation.
- Full rebuild command: every profile, tier, and score is recomputable from
  the fact tables, replayed in sent_at order.
- Encrypted storage: SQLCipher keyed from the OS keychain (plain-SQLite dev
  fallback with a visible warning).
- Localhost-only dashboard (127.0.0.1) with tier filters and per-message
  score decomposition.
- Test suite with synthetic mailbox fixtures: lookalike-domain phish,
  compromised-contact behavior shift, cold outreach, incremental sync,
  rebuild determinism.
