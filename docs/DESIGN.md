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
- Text parts larger than `MAX_TEXT_SCAN_BYTES` (1 MiB) are skipped for link
  extraction and recorded with `messages.links_extracted = 0`.
- Subjects, full URLs, attachment filenames, and full recipient lists are not
  persisted.
- Attachments are not downloaded by IMAP sync; `BODYSTRUCTURE` supplies MIME
  type, size, and filename metadata, and only the derived extension is stored.
- The Apple Mail store transport also avoids retaining attachment bytes; it
  decodes only bounded scannable text parts and keeps other parts size-only.
- Outgoing recipients are the documented exception to "no recipient lists":
  `message_recipients` stores links from outgoing messages to sender/contact
  rows so reply credits and Tier 3 are recomputable.
- Credentials, OAuth access tokens, and the database key live in the OS
  keychain. `NODARY_DB_KEY` is an override for tests/CI.
- With the `sqlcipher` extra installed, storage uses SQLCipher; otherwise it
  falls back to plain SQLite and records `schema_meta.encryption = none`.
- Public Suffix List lookup uses `tldextract`'s bundled snapshot with runtime
  fetching disabled. The freemail list and confusables subset are vendored in
  `feature_extraction.normalize`.

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
  dashboard page.

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
   `BATCH_SIZE = 200`. The high-water mark advances only through UIDs whose
   metadata was actually available; this prevents Apple Mail index rows whose
   `.emlx` files have not landed yet from being skipped forever.
6. The sync layer parses headers, walks structure, fetches only bounded text
   parts for link extraction, decides message direction, and calls
   `pipeline.ingest_message`.
7. A self-From message is outgoing only when it is in a sent folder or it is
   self-sent without a DMARC failure. A self-From message with DMARC fail is
   treated as incoming and scored.

## Storage Model

`schema.sql` groups data into four classes:

- Account/source state: `accounts`, `user_identities`, and `folders`.
  `accounts.auth_method` allows `oauth2`, `app_password`, and `mail_store`.
- Facts: `senders`, `threads`, `messages`, `message_attachments`,
  `message_link_domains`, and outgoing-only `message_recipients`.
- Derived profiles: `sender_profiles`, `sender_display_names`,
  `sender_attachment_types`, `sender_link_domains`, `sender_replyto_addrs`,
  `thread_reply_credits`, and `domain_profiles`.
- Scores: `message_scores` and `message_score_features`.

Derived tables are caches over message facts. `pipeline.rebuild()` deletes the
derived tables and replays all messages ordered by `(sent_at, id)` so profiles,
tiers, and scores are regenerated deterministically.

## Normalization

- Addresses are lowercased, `+tag` is stripped, and Gmail-family local-part
  dots are removed.
- Registrable domains come from the bundled `tldextract` Public Suffix List
  snapshot.
- Display names and registrable domains are casefolded through a small
  vendored UTS #39-style confusables table, plus common digit substitutions.
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
| 2 | At least 2 messages spanning at least 7 days, with no user reply/initiated thread | prior one-way contact |
| 1 | New sender, same non-freemail registrable domain as a domain with at least one replied thread | sender new, organization known |
| 0 | Everything else | never seen |

Freemail domains never propagate Tier 1 to unrelated senders.

## Scoring

The scoring registry is `src/nodary/scoring/registry.py`.
`ENGINE_VERSION = "1.0.0"` is stored on each score. Each feature returns a raw
value in `[0, 1]`; contribution is `raw * weight`; total score is capped at
100. Features are monotone, so normal-looking behavior never subtracts points
from another warning.

Identity/spoofing features apply at all tiers:

| Feature | Weight | Current behavior |
|---|---:|---|
| `lookalike_domain` | 25 | Fires when an untrusted non-own sender domain skeleton-collides with, or is edit-distance 1/2 from, a trusted non-freemail Tier >= 2 domain. |
| `display_name_collision` | 25 | Fires when a display-name skeleton matches a Tier 3 contact's name and the sender differs. |
| `auth_fail` | 15 | DMARC fail = 1.0; DKIM fail plus SPF fail/softfail = 0.8; SPF softfail = 0.4. |
| `reply_to_divergence` | 10 | For Tier >= 2 senders, fires when Reply-To uses a different registrable domain and the sender has not used that Reply-To before. |
| `embedded_addr_mismatch` | 10 | Fires when the display name contains an email-like token whose domain differs from the sender domain. |

Behavioral features apply only when `tier >= 2` and `baseline_n >= 8`.
Novelty signals use `confidence(n) = n / (n + 10)`:

| Feature | Weight | Current behavior |
|---|---:|---|
| `attachment_type_novelty` | 15 | Fires for extension/MIME pairs not seen from this sender. |
| `first_attachment_ever` | 10 | Fires instead of attachment-type novelty when the sender previously had no attachments. |
| `link_domain_novelty` | 10 | Fires on the fraction of message link domains not seen from this sender, only when link extraction completed. |
| `send_hour_anomaly` | 8 | Uses Laplace-smoothed sender-local hour history and `max(0, 1 - p * 24)`. |
| `link_density_anomaly` | 5 | One-sided z-score for unusually many links, clamped from z=2 to z=6. |
| `size_anomaly` | 5 | Two-sided z-score on log message size, clamped from z=2.5 to z=6.5. |
| `dormant_resurrection` | 5 | Fires only alongside another flag when the gap is at least 90 days and more than 6x the sender's median prior gap. The median is computed from facts on demand. |

Cold-contact features apply only when `tier <= 1` and `baseline_n < 8`.
They run at full strength for a first-ever sender and half strength for a
barely known sender:

| Feature | Weight | Current behavior |
|---|---:|---|
| `cold_attachment` | 12 | Attachment from a never-seen or barely known sender. |
| `cold_links` | 6 | One or more links from a never-seen or barely known sender. |
| `cold_replyto` | 8 | Divergent Reply-To from a never-seen or barely known sender. |

The dashboard orders incoming messages by anomaly score descending, then
`sent_at` descending. Tier filtering is available, but tier is not used as the
primary sort key.
