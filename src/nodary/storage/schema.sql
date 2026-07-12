PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id          INTEGER PRIMARY KEY,
  email       TEXT NOT NULL UNIQUE,
  imap_host   TEXT NOT NULL,
  imap_port   INTEGER NOT NULL DEFAULT 993,
  auth_method TEXT NOT NULL CHECK (auth_method IN ('oauth2','app_password')),
  created_at  INTEGER NOT NULL
  -- no secrets here: tokens/passwords live in the OS keychain,
  -- keyed by "nodary/account/<id>"
);

CREATE TABLE IF NOT EXISTS user_identities (
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  email_norm TEXT NOT NULL,
  PRIMARY KEY (account_id, email_norm)
);

CREATE TABLE IF NOT EXISTS folders (
  id             INTEGER PRIMARY KEY,
  account_id     INTEGER NOT NULL REFERENCES accounts(id),
  name           TEXT NOT NULL,
  role           TEXT NOT NULL CHECK (role IN ('inbox','sent','archive','other')),
  uidvalidity    INTEGER,
  last_seen_uid  INTEGER NOT NULL DEFAULT 0,
  last_synced_at INTEGER,
  UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS senders (
  id                  INTEGER PRIMARY KEY,
  email_norm          TEXT NOT NULL UNIQUE,
  domain              TEXT NOT NULL,
  reg_domain          TEXT NOT NULL,
  reg_domain_skeleton TEXT NOT NULL,
  is_freemail         INTEGER NOT NULL DEFAULT 0,
  first_seen_at       INTEGER,
  last_seen_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_senders_reg_domain ON senders(reg_domain);
CREATE INDEX IF NOT EXISTS idx_senders_skeleton   ON senders(reg_domain_skeleton);

CREATE TABLE IF NOT EXISTS threads (
  id              INTEGER PRIMARY KEY,
  root_message_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS messages (
  id                  INTEGER PRIMARY KEY,
  folder_id           INTEGER NOT NULL REFERENCES folders(id),
  uid                 INTEGER NOT NULL,
  message_id          TEXT,
  direction           TEXT NOT NULL CHECK (direction IN ('in','out')),
  sender_id           INTEGER REFERENCES senders(id),
  from_email_norm     TEXT NOT NULL,
  from_display_name   TEXT,
  reply_to_email_norm TEXT,
  to_me_directly      INTEGER NOT NULL DEFAULT 0,
  n_recipients        INTEGER,
  sent_at             INTEGER NOT NULL,
  sent_hour_local     INTEGER,
  sent_dow_local      INTEGER,
  size_bytes          INTEGER NOT NULL,
  n_attachments       INTEGER NOT NULL DEFAULT 0,
  n_links             INTEGER NOT NULL DEFAULT 0,
  links_extracted     INTEGER NOT NULL DEFAULT 1,  -- 0 when text parts were too large to scan
  is_reply            INTEGER NOT NULL DEFAULT 0,
  thread_id           INTEGER REFERENCES threads(id),
  thread_depth        INTEGER NOT NULL DEFAULT 0,
  auth_spf            TEXT,
  auth_dkim           TEXT,
  auth_dmarc          TEXT,
  UNIQUE (folder_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_msgid  ON messages(message_id);

CREATE TABLE IF NOT EXISTS message_attachments (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  mime_type  TEXT NOT NULL,
  extension  TEXT,
  size_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_att_msg ON message_attachments(message_id);

CREATE TABLE IF NOT EXISTS message_link_domains (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  reg_domain TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (message_id, reg_domain)
);

-- Outgoing mail only: which known contacts a sent message was addressed to.
-- Required to compute Tier 3 (two-way correspondence) and keep it recomputable.
CREATE TABLE IF NOT EXISTS message_recipients (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  sender_id  INTEGER NOT NULL REFERENCES senders(id),
  PRIMARY KEY (message_id, sender_id)
);

CREATE TABLE IF NOT EXISTS sender_profiles (
  sender_id           INTEGER PRIMARY KEY REFERENCES senders(id),
  n_messages          INTEGER NOT NULL DEFAULT 0,
  n_threads           INTEGER NOT NULL DEFAULT 0,
  n_replied_threads   INTEGER NOT NULL DEFAULT 0,
  n_user_initiated    INTEGER NOT NULL DEFAULT 0,
  trust_tier          INTEGER NOT NULL DEFAULT 0,
  hour_histogram      BLOB NOT NULL,
  dow_histogram       BLOB NOT NULL,
  log_size_mean       REAL,
  log_size_m2         REAL,
  links_mean          REAL,
  links_m2            REAL,
  n_with_attachments  INTEGER NOT NULL DEFAULT 0,
  n_with_links        INTEGER NOT NULL DEFAULT 0,
  n_replyto_divergent INTEGER NOT NULL DEFAULT 0,
  first_msg_at        INTEGER,
  last_msg_at         INTEGER,
  max_thread_depth    INTEGER NOT NULL DEFAULT 0,
  updated_at          INTEGER NOT NULL,
  profile_version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sender_display_names (
  sender_id     INTEGER NOT NULL REFERENCES senders(id),
  name_norm     TEXT NOT NULL,
  name_skeleton TEXT NOT NULL,
  n             INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (sender_id, name_norm)
);
CREATE INDEX IF NOT EXISTS idx_names_skeleton ON sender_display_names(name_skeleton);

CREATE TABLE IF NOT EXISTS sender_attachment_types (
  sender_id     INTEGER NOT NULL REFERENCES senders(id),
  extension     TEXT NOT NULL,
  mime_type     TEXT NOT NULL,
  n             INTEGER NOT NULL DEFAULT 1,
  first_seen_at INTEGER NOT NULL,
  PRIMARY KEY (sender_id, extension, mime_type)
);

CREATE TABLE IF NOT EXISTS sender_link_domains (
  sender_id     INTEGER NOT NULL REFERENCES senders(id),
  reg_domain    TEXT NOT NULL,
  n             INTEGER NOT NULL DEFAULT 1,
  first_seen_at INTEGER NOT NULL,
  PRIMARY KEY (sender_id, reg_domain)
);

CREATE TABLE IF NOT EXISTS sender_replyto_addrs (
  sender_id  INTEGER NOT NULL REFERENCES senders(id),
  email_norm TEXT NOT NULL,
  n          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (sender_id, email_norm)
);

-- Which threads already credited a reply to a sender (prevents double count).
CREATE TABLE IF NOT EXISTS thread_reply_credits (
  thread_id INTEGER NOT NULL REFERENCES threads(id),
  sender_id INTEGER NOT NULL REFERENCES senders(id),
  PRIMARY KEY (thread_id, sender_id)
);

CREATE TABLE IF NOT EXISTS domain_profiles (
  reg_domain        TEXT PRIMARY KEY,
  n_senders         INTEGER NOT NULL DEFAULT 0,
  n_messages        INTEGER NOT NULL DEFAULT 0,
  n_replied_threads INTEGER NOT NULL DEFAULT 0,
  is_freemail       INTEGER NOT NULL DEFAULT 0,
  first_seen_at     INTEGER,
  last_seen_at      INTEGER
);

CREATE TABLE IF NOT EXISTS message_scores (
  message_id            INTEGER PRIMARY KEY
                        REFERENCES messages(id) ON DELETE CASCADE,
  engine_version        TEXT NOT NULL,
  trust_tier_at_scoring INTEGER NOT NULL,
  baseline_n            INTEGER NOT NULL,
  anomaly_score         REAL NOT NULL,
  scored_at             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS message_score_features (
  message_id   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  feature      TEXT NOT NULL,
  raw_value    REAL NOT NULL,
  weight       REAL NOT NULL,
  contribution REAL NOT NULL,
  explanation  TEXT NOT NULL,
  PRIMARY KEY (message_id, feature)
);
