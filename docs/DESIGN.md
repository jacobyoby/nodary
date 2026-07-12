# Nodary — Design Proposal v0.1: Storage Schema & Feature Vector

Status: **for review** — nothing below is implemented yet. The sync engine starts
after this document is approved.

## 1. Privacy invariants the schema must uphold

1. **No body text is ever persisted.** Bodies are streamed through a structural
   parser that extracts only: URL hostnames, attachment MIME types/extensions,
   and byte counts. The parse buffer is discarded per message.
2. **No filenames, subjects, or recipient lists are stored** — only counts and
   derived structural values. (Subject is not needed by any v1 feature.)
3. **The database is encrypted at rest** with SQLCipher. The key is a random
   256-bit value generated at first run, stored in the OS keychain (macOS
   Keychain / freedesktop Secret Service / Windows Credential Manager via
   `keyring`), never on disk. IMAP credentials/OAuth tokens also live only in
   the keychain — the `accounts` table holds connection metadata only.
4. **Everything derived is recomputable.** Aggregates (`sender_profiles`,
   `domain_profiles`) are caches over the `messages` fact table; a
   `rebuild-profiles` command can regenerate them, which lets us change the
   profile format without re-downloading mail.

All timestamps are UTC unix epoch seconds (`INTEGER`). All email addresses are
stored normalized (§4).

## 2. SQLite schema (SQLCipher)

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (          -- schema_version, engine_version, freemail_list_version
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------- accounts --
CREATE TABLE accounts (
  id          INTEGER PRIMARY KEY,
  email       TEXT NOT NULL UNIQUE,          -- the user's own primary address
  imap_host   TEXT NOT NULL,
  imap_port   INTEGER NOT NULL DEFAULT 993,
  auth_method TEXT NOT NULL CHECK (auth_method IN ('oauth2','app_password')),
  created_at  INTEGER NOT NULL
  -- no secrets here: tokens/passwords live in the OS keychain,
  -- keyed by "nodary/account/<id>"
);

CREATE TABLE user_identities (      -- every address that counts as "me"
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  email_norm TEXT NOT NULL,                  -- aliases, send-as addresses
  PRIMARY KEY (account_id, email_norm)
);

CREATE TABLE folders (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  name          TEXT NOT NULL,               -- IMAP mailbox name (UTF-7 decoded)
  role          TEXT NOT NULL CHECK (role IN ('inbox','sent','archive','other')),
  uidvalidity   INTEGER,                     -- NULL until first sync
  last_seen_uid INTEGER NOT NULL DEFAULT 0,  -- high-water mark; fetch (last_seen_uid, *]
  last_synced_at INTEGER,
  UNIQUE (account_id, name)
);
-- Incremental sync contract: if server UIDVALIDITY != folders.uidvalidity,
-- the folder's messages are invalidated and re-fetched (headers/structure only,
-- so even a full resync never re-downloads bodies beyond BODYSTRUCTURE + the
-- MIME parts needed for link extraction).

-- ----------------------------------------------------------------- senders --
CREATE TABLE senders (
  id                  INTEGER PRIMARY KEY,
  email_norm          TEXT NOT NULL UNIQUE,
  domain              TEXT NOT NULL,         -- full domain part
  reg_domain          TEXT NOT NULL,         -- registrable domain via Public Suffix List
  reg_domain_skeleton TEXT NOT NULL,         -- UTS #39 confusable-skeleton of reg_domain
  is_freemail         INTEGER NOT NULL DEFAULT 0,  -- gmail.com etc.; blocks Tier-1 propagation
  first_seen_at       INTEGER,
  last_seen_at        INTEGER
);
CREATE INDEX idx_senders_reg_domain ON senders(reg_domain);
CREATE INDEX idx_senders_skeleton   ON senders(reg_domain_skeleton);

-- ---------------------------------------------------------------- messages --
-- One row per message in a synced folder, incoming AND outgoing (Sent folder
-- sync is mandatory: reply-rate and Tier 3 cannot be computed without it).
CREATE TABLE threads (
  id              INTEGER PRIMARY KEY,
  root_message_id TEXT UNIQUE                -- earliest Message-ID observed in the chain
);

CREATE TABLE messages (
  id                  INTEGER PRIMARY KEY,
  folder_id           INTEGER NOT NULL REFERENCES folders(id),
  uid                 INTEGER NOT NULL,
  message_id          TEXT,                  -- RFC 5322 Message-ID (nullable, not unique in the wild)
  direction           TEXT NOT NULL CHECK (direction IN ('in','out')),
  sender_id           INTEGER REFERENCES senders(id),  -- NULL when direction='out'
  from_email_norm     TEXT NOT NULL,
  from_display_name   TEXT,                  -- raw, as sent (needed for collision evidence)
  reply_to_email_norm TEXT,                  -- NULL if absent or identical to From
  to_me_directly      INTEGER NOT NULL DEFAULT 0,  -- a user identity appears in To (vs Cc/list)
  n_recipients        INTEGER,
  sent_at             INTEGER NOT NULL,      -- Date header → UTC
  sent_hour_local     INTEGER,               -- 0-23 in the SENDER's own UTC offset (from Date hdr)
  sent_dow_local      INTEGER,               -- 0-6, same clock
  size_bytes          INTEGER NOT NULL,      -- RFC822.SIZE
  n_attachments       INTEGER NOT NULL DEFAULT 0,
  n_links             INTEGER NOT NULL DEFAULT 0,
  is_reply            INTEGER NOT NULL DEFAULT 0,   -- has In-Reply-To
  thread_id           INTEGER REFERENCES threads(id),
  thread_depth        INTEGER NOT NULL DEFAULT 0,   -- position in reference chain
  auth_spf            TEXT,                  -- 'pass'|'fail'|'softfail'|'none'|NULL
  auth_dkim           TEXT,                  --   parsed from Authentication-Results
  auth_dmarc          TEXT,                  --   (server-recorded; still purely local data)
  UNIQUE (folder_id, uid)
);
CREATE INDEX idx_messages_sender ON messages(sender_id, sent_at);
CREATE INDEX idx_messages_thread ON messages(thread_id);

CREATE TABLE message_attachments (  -- structure only; NO filename stored
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  mime_type  TEXT NOT NULL,                  -- from BODYSTRUCTURE
  extension  TEXT,                           -- lowercased ext parsed from filename, then filename discarded
  size_bytes INTEGER
);
CREATE INDEX idx_att_msg ON message_attachments(message_id);

CREATE TABLE message_link_domains ( -- hostnames only; full URLs are never stored
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  reg_domain TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (message_id, reg_domain)
);

-- --------------------------------------------------- derived: sender profile --
-- Incrementally maintained on ingest; fully recomputable from messages.
CREATE TABLE sender_profiles (
  sender_id           INTEGER PRIMARY KEY REFERENCES senders(id),
  n_messages          INTEGER NOT NULL DEFAULT 0,
  n_threads           INTEGER NOT NULL DEFAULT 0,
  n_replied_threads   INTEGER NOT NULL DEFAULT 0,  -- threads where the user replied to this sender
  n_user_initiated    INTEGER NOT NULL DEFAULT 0,  -- threads the user started
  trust_tier          INTEGER NOT NULL DEFAULT 0,  -- 0..3, see §5
  hour_histogram      BLOB NOT NULL,        -- 24 × uint32 LE, sender-local clock
  dow_histogram       BLOB NOT NULL,        -- 7 × uint32 LE
  log_size_mean       REAL,                 -- Welford running mean of ln(size_bytes)
  log_size_m2         REAL,                 -- Welford M2 (→ variance)
  links_mean          REAL,
  links_m2            REAL,
  n_with_attachments  INTEGER NOT NULL DEFAULT 0,
  n_with_links        INTEGER NOT NULL DEFAULT 0,
  n_replyto_divergent INTEGER NOT NULL DEFAULT 0,  -- msgs where Reply-To reg_domain ≠ From reg_domain
  median_gap_seconds  REAL,                 -- typical inter-arrival time (P² estimator)
  max_thread_depth    INTEGER NOT NULL DEFAULT 0,
  updated_at          INTEGER NOT NULL,
  profile_version     INTEGER NOT NULL      -- bump forces rebuild
);

CREATE TABLE sender_display_names (
  sender_id      INTEGER NOT NULL REFERENCES senders(id),
  name_norm      TEXT NOT NULL,             -- casefolded, whitespace-collapsed
  name_skeleton  TEXT NOT NULL,             -- UTS #39 skeleton (homoglyph-folded)
  n              INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (sender_id, name_norm)
);
CREATE INDEX idx_names_skeleton ON sender_display_names(name_skeleton);

CREATE TABLE sender_attachment_types (
  sender_id  INTEGER NOT NULL REFERENCES senders(id),
  extension  TEXT NOT NULL,                 -- '' when only MIME known
  mime_type  TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 1,
  first_seen_at INTEGER NOT NULL,
  PRIMARY KEY (sender_id, extension, mime_type)
);

CREATE TABLE sender_link_domains (
  sender_id  INTEGER NOT NULL REFERENCES senders(id),
  reg_domain TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 1,
  first_seen_at INTEGER NOT NULL,
  PRIMARY KEY (sender_id, reg_domain)
);

CREATE TABLE sender_replyto_addrs (
  sender_id  INTEGER NOT NULL REFERENCES senders(id),
  email_norm TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (sender_id, email_norm)
);

-- ---------------------------------------------------- derived: domain graph --
CREATE TABLE domain_profiles (
  reg_domain        TEXT PRIMARY KEY,
  n_senders         INTEGER NOT NULL DEFAULT 0,
  n_messages        INTEGER NOT NULL DEFAULT 0,
  n_replied_threads INTEGER NOT NULL DEFAULT 0,
  is_freemail       INTEGER NOT NULL DEFAULT 0,
  first_seen_at     INTEGER,
  last_seen_at      INTEGER
);

-- ------------------------------------------------------------------ scoring --
CREATE TABLE message_scores (
  message_id            INTEGER PRIMARY KEY REFERENCES messages(id),
  engine_version        TEXT NOT NULL,      -- feature registry + weights version
  trust_tier_at_scoring INTEGER NOT NULL,   -- tier can change later; keep what UI showed
  baseline_n            INTEGER NOT NULL,   -- profile size the score was judged against
  anomaly_score         REAL NOT NULL,      -- 0..100, capped weighted sum
  scored_at             INTEGER NOT NULL
);

CREATE TABLE message_score_features (       -- full decomposition = the explanation
  message_id   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  feature      TEXT NOT NULL,               -- registry name (§6)
  raw_value    REAL NOT NULL,               -- normalized 0..1
  weight       REAL NOT NULL,               -- points at raw_value = 1.0
  contribution REAL NOT NULL,               -- raw_value × weight
  explanation  TEXT NOT NULL,               -- rendered template, e.g.
                                            -- "first .zip attachment in 214 messages from this sender"
  PRIMARY KEY (message_id, feature)
);
```

## 3. What gets read from the server (and what doesn't)

Per message the sync engine fetches: `UID`, `RFC822.SIZE`, `BODYSTRUCTURE`,
`FLAGS`, and header fields `From To Cc Date Message-ID In-Reply-To References
Reply-To Authentication-Results Content-Type`. Text parts are fetched **only**
to run URL-hostname extraction, then dropped; `text/*` parts over 1 MiB are
skipped (link stats marked "not extracted"). Attachments are never fetched —
`BODYSTRUCTURE` already carries MIME type, size, and filename (we keep only
the extension).

## 4. Normalization rules (deterministic, versioned)

- **Address:** casefold; strip `+tag`; for Gmail-family domains also strip
  dots in the local part. Raw From is preserved on the message row.
- **Registrable domain:** Public Suffix List (vendored snapshot, versioned in
  `schema_meta` — no runtime fetch).
- **Skeleton:** Unicode UTS #39 confusables skeleton, applied to display names
  and reg_domains. `paypaІ.com` (Cyrillic І) and `paypal.com` collide in
  skeleton space; that collision *is* the lookalike signal.
- **Sender-local time:** the Date header carries the sender's UTC offset —
  `sent_hour_local` uses *their* clock, so "hours they never write" tracks the
  human, not our timezone.

## 5. Trust tiers (computed, never manually set in v1)

| Tier | Rule (first match wins, evaluated top-down) |
|------|---------------------------------------------|
| 3 | `n_replied_threads ≥ 1` OR `n_user_initiated ≥ 1` — established two-way correspondence |
| 2 | `n_messages ≥ 2` over ≥ 7 days, no reply from user — prior one-way contact |
| 1 | sender new, but `domain_profiles[reg_domain].n_replied_threads ≥ 1` AND NOT freemail — the *organization* is known |
| 0 | everything else |

Freemail exclusion is load-bearing: `random@gmail.com` must not inherit Tier 1
because you correspond with someone else at gmail.com. Shipped as a vendored
static list (~200 domains), versioned.

## 6. Feature vector

Every feature: deterministic, normalized to **[0, 1]**, with a named weight
(points contributed at 1.0) and an explanation template. Score =
`min(100, Σ raw×weight)`. Weights live in one versioned registry module —
changing any weight bumps `engine_version`.

**Confidence gate:** behavioral features are meaningless against thin
baselines. Each novelty feature is multiplied by `conf(n) = n / (n + 10)`, so
"first attachment in 214 messages" ≈ full strength (0.96) while "first
attachment in 4 messages" is nearly muted (0.29). Group B features emit 0 with
explanation "baseline too small (n)" when `n_messages < 8`.

### Group A — identity & spoofing (all tiers)

| feature | weight | fires when | explanation template |
|---|---|---|---|
| `lookalike_domain` | 25 | sender reg_domain ≠ but skeleton-collides with (or is Damerau-Levenshtein ≤ 2 from, min length 6) a Tier ≥ 2 domain | "domain ‹micros0ft.com› resembles known domain ‹microsoft.com›" |
| `display_name_collision` | 25 | display-name skeleton matches a name used by a Tier 3 contact, but address differs | "display name matches ‹Dana Ito ‹dana@acme.com›› but address is ‹dana.ito@mail-acme.net›" |
| `auth_fail` | 15 | DMARC fail (1.0) / DKIM+SPF both fail (0.8) / softfail (0.4), from Authentication-Results | "DMARC failed for sending domain" |
| `reply_to_divergence` | 10 | Reply-To reg_domain ≠ From reg_domain AND sender has never used this Reply-To before | "replies redirect to ‹collect@other-domain.ru›, never seen from this sender" |
| `embedded_addr_mismatch` | 10 | display name contains an email-like token whose domain ≠ From domain | "display name shows ‹ceo@acme.com› but real sender is ‹x@evil.net›" |

### Group B — behavioral shift vs sender's own baseline (Tier ≥ 2, n ≥ 8)

| feature | weight | raw value | explanation template |
|---|---|---|---|
| `attachment_type_novelty` | 15 | 1.0 × conf(n) if extension/MIME pair never seen from sender | "first ‹.zip› from this sender in 214 messages" |
| `first_attachment_ever` | 10 | 1.0 × conf(n) if sender's attachment count was 0 (subsumes `attachment_type_novelty`: only the larger fires) | "first attachment of any kind in 214 messages" |
| `link_domain_novelty` | 10 | (novel link domains ÷ link domains in msg) × conf(n) | "links to ‹dropbox-files.net›, never linked before (0 of 87 prior link domains)" |
| `send_hour_anomaly` | 8 | surprisal of hour bucket under Laplace-smoothed histogram, scaled: `max(0, 1 − p·24)` clamped | "sent at 03:00 sender-local; 0 of 214 prior messages in 02:00–05:00" |
| `link_density_anomaly` | 5 | `clamp((z − 2) / 4)` where z = links z-score | "14 links; sender's typical is 0.4 ± 0.9" |
| `size_anomaly` | 5 | `clamp((|z| − 2.5) / 4)` on ln(size) | "412 KB message; sender's typical is 6 KB" |
| `dormant_resurrection` | 5 | gap > 6 × median_gap AND ≥ 90 days, only when any other Group A/B feature fired | "first message in 14 months, combined with other anomalies" |

### Group C — cold-contact context (Tier 0–1 only; these contextualize, not accuse)

| feature | weight | fires when | explanation template |
|---|---|---|---|
| `cold_attachment` | 12 | first-ever message includes an attachment | "attachment from a never-seen sender" |
| `cold_links` | 6 | first-ever message includes ≥ 1 link | "3 links from a never-seen sender" |
| `cold_replyto` | 8 | first-ever message sets divergent Reply-To | "never-seen sender redirects replies elsewhere" |

Group A and C can co-fire (a lookalike cold sender with an attachment stacks
to ~62 points). Group B never fires for Tier 0/1 — there is no baseline to
betray. The dashboard sorts by `(trust_tier ASC is *not* used directly)` — it
buckets by tier, then orders by anomaly score descending within bucket, so a
Tier 3 contact behaving strangely surfaces above routine Tier 0 newsletters.

## 7. Scoring properties worth stating

- **Deterministic:** same message + same profile state → same score, bit-for-bit.
  Tests assert this.
- **Explainable by construction:** the score *is* the sum of
  `message_score_features` rows; the UI renders those rows verbatim. There is
  no hidden term.
- **Monotone:** no feature can lower a score. Absence of anomaly = 0 points,
  not negative points (prevents attackers from *buying* trust by looking extra
  normal in some dimension).
- **Order-independent-ish:** profiles are built from history *before* the
  scored message; rescoring after a full resync yields identical results
  because messages are replayed in `sent_at` order during profile rebuild.

## 8. Open questions for review

1. **Freemail Tier-1 blocking** (§5) — agreed? Alternative: allow Tier 1 for
   freemail only on exact local-part similarity, but that adds complexity for
   marginal benefit.
2. **Baseline thresholds** — `n ≥ 8` to activate Group B, `conf(n) = n/(n+10)`.
   Tunable constants in the registry; are these starting points acceptable?
3. **SQLCipher via `sqlcipher3-wheels`** (bundled binary) vs `pysqlcipher3`
   (build from source). Proposal: `sqlcipher3-wheels` for install ergonomics;
   key from OS keychain via `keyring`.
4. **Sent-folder sync is mandatory** for Tier 3 / reply-rate. If a provider
   blocks it, senders cap at Tier 2 and the UI says why. OK?
5. **Large text parts** (> 1 MiB) skip link extraction (§3) to keep sync fast
   on 100k mailboxes. The message is marked `links not extracted` rather than
   silently scoring 0 link features. OK?
