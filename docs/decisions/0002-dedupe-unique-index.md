# ADR 0002: Partial unique index for message deduplication

Date: 2026-09-20
Status: Accepted

## Context

`pipeline.py` deduped via SELECT on `(message_id, sent_at, size_bytes)` then INSERT — classic check-then-act race. Forwarded mail (same Message-ID in two folders/accounts) and rapid sync could double-insert, double-counting sender baselines and scores.

## Decision

- Add `CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe ON messages(message_id, sent_at, size_bytes) WHERE message_id IS NOT NULL` to `storage/schema.sql`.
- Add migration `006_dedupe_index.py` (idempotent) and bump `LATEST_VERSION=6`.
- Make `ingest_message` catch `sqlite3.IntegrityError` from the race, re-lookup `SELECT id ...` and return existing row id.

## Consequences

- Deduplication is now DB-enforced; `rebuild()` remains deterministic.
- Mailing-list reuse of Message-ID still separates via differing `sent_at`/`size_bytes`.

