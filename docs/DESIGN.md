# Nodary Design

Status: **current implementation, v0.3.0**. The database schema in
`src/nodary/storage/schema.sql` is authoritative for table/column details.

Nodary is a local-first, deterministic behavioral-analysis tool for BEC and
phishing review. It builds sender baselines from local mailbox facts, scores
incoming messages as a weighted sum of named features, and serves a
localhost-only dashboard. It does not send mail, modify mailbox contents, call
cloud scoring APIs, or emit telemetry.

## Privacy Invariants

- Body text is transient. Text/plain and text/html parts are read only to
  extract HTTP(S) link hostnames, then discarded.
- Text parts larger than `MAX_TEXT_SCAN_BYTES` (1 MiB), or whose transfer
  encoding cannot be decoded, are skipped for link extraction and recorded
  with `messages.links_extracted = 0`.
- Subjects, full URLs, attachment filenames, and full recipient lists are not
  persisted.
- Attachments are not downloaded by IMAP sync; `BODYSTRUCTURE` supplies MIME
  type, size, and filename metadata, and only the derived extension is stored.
- The Apple Mail store transport also avoids retaining attachment bytes; it
  decodes only bounded scannable text parts and keeps other parts size-only.
- Permanently unparseable `.emlx` files are recorded in `skipped_messages`
  with folder, rowid, filesystem path, and a short reason (framing error or
  exception type). Message bytes are not stored.
- Outgoing recipients are the documented exception to "no recipient lists":
  `message_recipients` stores links from outgoing messages to sender/contact
  rows so reply credits and Tier 3 are recomputable.
- Credentials, OAuth access tokens, and the database key live in the OS
  keychain. `NODARY_DB_KEY` is an override for tests/CI.
- With the `sqlcipher` extra installed, storage uses SQLCipher; otherwise it
  falls back to plain SQLite and records `schema_meta.encryption = none`.
- Public Suffix List lookup uses `tldextract`'s bundled snapshot with runtime
  fetching disabled. The freemail list is vendored in `feature_extraction.normalize`;
  the confusables map is generated at build time from vendored UTS #39 data
  (`data/uts39/confusables.txt`) — no Unicode downloads at runtime.

## Dashboard Sync-Health UI

The dashboard shows a sync-health status strip between the header and tier
filters. The strip contains a color-coded dot (green = healthy, yellow =
skipped messages exist, red = per-account errors), a plain-language summary
of permanent skip counts and per-account errors, and per-account pills
showing last sync time.

Skip counts come from the `skipped_messages` table, which records folder,
UID, reason, and timestamp for messages the sync layer could not ingest
(e.g. missing `.emlx`, unparseable headers). No message content is stored.
Last sync time is derived from `folders.last_synced_at` (MAX per account).
Last error is a persisted string on `accounts.last_error`, written by the
sync layer when an account-level failure occurs (e.g. missing credential).

The skip count links to a local-only overlay listing skipped messages via
`/api/skipped`. The overlay shows account, folder name, UID, reason, and
timestamp — no message bodies, subjects, or sender addresses.

## Dashboard Account Filtering

The dashboard supports viewing data for a single account or all accounts
(default). An account switcher dropdown is populated from `/api/accounts`
(id + email for every configured account, plus an "all accounts" option).
Switching accounts updates the message list and status strip without a
page reload.

`/api/messages` and `/api/status` accept `?account=<id>` or `?account=all`
(default). When filtered to a single account, the endpoints join through
`folders.account_id` to restrict results. The account filter composes with
existing tier and limit parameters. A nonexistent account id returns 404.

When viewing all accounts, the status strip shows per-account pills with
last sync time and skip count. When viewing a single account, only that
account's pill is shown and all counts reflect only that account's data.

## Main Components

- `cli.py` provides `add-account`, `set-secret`, `set-source`, `sync`,
  `rebuild`, and `ui`.
- `imap_sync.client.ImapTransport` is the direct IMAP source. It selects
  folders read-only and fetches `BODY.PEEK[HEADER]`, `RFC822.SIZE`, and
  `BODYSTRUCTURE`.
- `mail_store.MailStoreTransport` is the local Apple Mail source. It opens the
  Envelope Index read-only, resolves `.emlx`/`.partial.emlx` files, and exposes
  the same transport protocol as IMAP.
- `imap_sync.sync` owns folder selection, UIDVALIDITY/high-water-mark sync,
  bounded text-part fetch, direction detection, and handoff to the pipeline.
- `feature_extraction.extract` converts headers plus structure/text snippets
  into a `MessageRecord`.
- `pipeline.py` persists facts, scores incoming mail against the prior sender
  snapshot, updates profiles, credits outgoing correspondence, and rebuilds
  derived tables.
- `scoring.registry`, `scoring.engine`, and `scoring.tiers` define feature
  weights, scoring behavior, and trust tiers.
- `ui.server` exposes local JSON endpoints and renders the self-contained
  dashboard page, including a sender-baseline drill-down.

## Sync Data Flow

1. `nodary sync` opens the local database, then processes each account.
2. IMAP accounts authenticate with app password or a stored OAuth2 access
   token. Mail-store accounts are matched to Apple Mail accounts by counting
   messages sent from the account's primary identity, then aliases as fallback;
   one store UUID can be claimed by only one nodary account per sync.
3. The selected transport lists sync folders. Sent folders are processed before
   inbox-like folders so outgoing relationship evidence exists before incoming
   scoring during an initial backfill.
4. Each folder stores `uidvalidity`, `last_seen_uid`, and `last_synced_at`.
   A UIDVALIDITY change deletes facts for that folder, resets its high-water
   mark, refetches, and triggers a full rebuild.
5. Normal sync requests UIDs above `last_seen_uid` in batches of
   `BATCH_SIZE = 200`. `fetch_meta` returns successful messages plus any
   permanent failures (a present `.emlx` that raises `EmlxError` or a parse
   exception). Transient gaps — a missing file, or a `.partial.emlx` that
   cannot be parsed yet — are omitted so the high-water mark stops and retries.
   Permanent failures are written to `skipped_messages` (folder, rowid, path,
   reason; no message content) and the high-water mark advances past them.
6. The sync layer parses headers, walks structure, fetches only bounded text
   parts for link extraction, decides message direction, and calls
   `pipeline.ingest_message`.
7. A self-From message is outgoing only when it is in a sent folder or it is
   self-sent without a DMARC failure. A self-From message with DMARC fail is
   treated as incoming and scored.

## Storage Model

`schema.sql` groups data into four classes:

- Account/source state: `accounts`, `user_identities`, `folders`, and
  `skipped_messages` (permanent mail-store fetch failures).
  `accounts.auth_method` allows `oauth2`, `app_password`, and `mail_store`.
- Facts: `senders`, `threads`, `messages`, `message_attachments`,
  `message_link_domains`, and outgoing-only `message_recipients`.
- Derived profiles: `sender_profiles`, `sender_display_names`,
  `sender_attachment_types`, `sender_link_domains`, `sender_replyto_addrs`,
  `thread_reply_credits`, and `domain_profiles`.
- Scores: `message_scores` and `message_score_features`.
- Sync health: `skipped_messages` (permanently skipped messages with
  folder, UID, and reason; no message content).

Derived tables are caches over message facts. `pipeline.rebuild()` deletes the
derived tables and replays all messages ordered by `(sent_at, id)` so profiles,
tiers, and scores are regenerated deterministically.

## Normalization

- Addresses are lowercased, `+tag` is stripped, and Gmail-family local-part
  dots are removed.
- Registrable domains come from the bundled `tldextract` Public Suffix List
  snapshot.
- Display names and registrable domains are casefolded through a confusables
  map generated from vendored UTS #39 data (`data/uts39/confusables.txt`,
  Unicode 17.0.0) by `scripts/generate_confusables.py`, plus a hand-audited
  curated subset (digit substitutions and Latin-target Cyrillic/Greek
  mappings) that overrides the generated entries where they differ. The
  curated subset is also the fallback when the generated module is absent.
  To regenerate after updating the vendored data:
  `python scripts/generate_confusables.py`.
- Sender-local hour/day come from the UTC offset carried in the `Date` header,
  so behavioral baselines follow the sender's clock rather than the user's.
- Authentication verdicts are parsed from the receiving server's
  `Authentication-Results` header.

## Pipeline Semantics

Incoming messages:

1. Resolve or create a thread using `In-Reply-To` and `References`.
2. Upsert the sender row.
3. Insert the message fact rows.
4. Load the sender profile snapshot as it existed before this message.
5. Compute the current trust tier from that snapshot.
6. Store score and feature rows.
7. Update sender/domain profiles and store the sender's new tier.

Outgoing messages:

1. Insert the outgoing message fact row.
2. Upsert each normalized recipient as a sender/contact and store
   `message_recipients`.
3. Credit replied threads for prior incoming senders in the same thread.
4. Credit user-initiated threads for recipients not already represented by
   prior incoming messages in that thread.
5. Recompute tiers for credited senders.

This ordering matters: incoming scoring always evaluates a message against the
baseline before that message contributed to the profile.

## Trust Tiers

Trust tiers are computed, not manually assigned. The first matching rule wins:

| Tier | Rule | Label |
|---:|---|---|
| 3 | `n_replied_threads >= 1` or `n_user_initiated >= 1` | established |
| 2 | Prior one-way contact spread over time, with no user reply/initiated thread | prior one-way contact |
| 1 | New sender, same non-freemail registrable domain as a domain with at least one replied thread | sender new, organization known |
| 0 | Everything else | never seen |

Freemail domains never propagate Tier 1 to unrelated senders.

## Scoring

The scoring registry is `src/nodary/scoring/registry.py`. Each feature returns a
raw value in `[0, 1]`, contributions are weighted and summed, and the total is
capped at 100. Features are monotone, so normal-looking behavior never subtracts
points from another warning. Features fall into three families — identity and
spoofing (all tiers), behavioral novelty against the sender's own history
(established senders only), and cold-contact context (new senders only).

The exact feature weights, history gates, confidence curve and anomaly
thresholds are deliberately not documented here: published to the letter they
read as a checklist for staying under each line. The registry is the source of
truth and is versioned by `ENGINE_VERSION`.

## Dashboard

The dashboard is one self-contained HTML document on `127.0.0.1` (TLS via local
mkcert when available). It makes no outbound requests and ships no external
assets.

The scored-message list still expands in place for per-feature contribution
bars. From a message row — the sender name, or **sender baseline** in the
expanded flags — the same page swaps to a local-only sender detail view
(`#sender/<id>`). That view is the explainability surface for behavioral
features: it shows the current trust tier and the rule that matched, message
counts and dated span, the sender-local send-hour histogram, typical size and
link-density from the Welford running stats, known attachment types
(extension + MIME only), known link domains, the known Reply-To set, and
recent scored messages with feature chips.

`GET /api/senders/<id>` and `GET /api/messages` read existing profile and
score tables only. Payloads omit subjects, filenames, full URLs, and body
text. No new message content is persisted. Multi-account filtering and
per-sender list collapse are separate concerns.
