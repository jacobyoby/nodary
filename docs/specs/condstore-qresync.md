# CONDSTORE / QRESYNC change-aware sync — Specification

Status: **specification** (SPARC-S). Not implemented.

## Goal

Use IMAP CONDSTORE (RFC 4551) and QRESYNC (RFC 7162) where the server
advertises them so per-folder sync is change-aware instead of UID range
scans, for large mailboxes. Pure performance change: ingested facts,
profiles, tiers, and scores are identical to the current path.

## Background

`src/nodary/imap_sync/sync.py::sync_folder` currently keeps
`(uidvalidity, last_seen_uid)` per folder in `folders`, discovers new
mail with `Transport.new_uids(after_uid)` (a `UID after+1:*` SEARCH),
and reconciles deletions with a full `new_uids(0)` UID listing mapped
to `messages.deleted_upstream`. `Transport` (`client.py`) exposes
`select_readonly` returning only `{uidvalidity, uidnext}`. There is no
capability detection, no MODSEQ state, and no QRESYNC support anywhere
in `src`, `tests`, or `docs`.

## Requirements

1. Capability-gated: `ENABLE CONDSTORE` / QRESYNC is attempted only
   when the server advertises it. Servers without it (and the
   mail-store transport, which has no MODSEQ concept) use the current
   UID-scan path unchanged, bit-for-bit.
2. Read-only preserved: select stays `readonly=True`; the new path
   never issues `STORE`, `EXPUNGE`, `MOVE`, or flag writes. `ENABLE
   CONDSTORE` and `SELECT ... (QRESYNC ...)` are read-only negotiation.
3. Persist per-folder sync state: `folders.highest_modseq INTEGER NULL`
   (`NULL` = unknown / unsupported / not yet observed). Set from the
   `HIGHESTMODSEQ` SELECT response after each folder sync.
4. Change-aware discovery: when a stored `highest_modseq` exists and
   the server supports it, discover new mail with `CHANGEDSINCE` /
   QRESYNC instead of the `UID after+1:*` scan.
5. Vanished handling: QRESYNC `VANISHED` responses mark the same
   `messages.deleted_upstream = 1` rows the full-listing reconcile
   would mark. Rows are never deleted; reappeared UIDs are un-marked,
   as today.
6. UIDVALIDITY semantics unchanged: on mismatch, wipe folder facts via
   the existing `_invalidate_folder` path and clear `highest_modseq`
   to `NULL` alongside `last_seen_uid = 0`.
7. Rebuildable: `nodary rebuild` regenerates identical state; MODSEQ
   values are sync watermarks only, never scoring inputs.

## Non-goals

- Flag / `\Seen` sync. MODSEQ use is limited to new-message discovery
  and expunge detection.
- IMAP IDLE (separate TODO item).
- Scoring, registry, weights, or `ENGINE_VERSION` changes.
- Mail-store transport changes beyond reporting "no CONDSTORE support".

## Transport interface (additive, backward compatible)

- `select_readonly` result gains optional `highest_modseq` when the
  server returns it; existing `{uidvalidity, uidnext}` keys unchanged.
- New optional capability probe, e.g. `condstore_supported() -> bool`
  with a default that returns `False`, so `FakeTransport` and the
  mail-store transport keep working without modification.
- New optional vanished-aware discovery method used only when the
  probe is true; the current `new_uids` path remains the fallback.
- Exact method names are an Architecture-phase decision; the constraint
  is additive-only, no breaking change to existing implementers.

## Schema

- Migration `006` adds `folders.highest_modseq INTEGER NULL`,
  idempotent (`PRAGMA table_info` check), following
  `docs/DESIGN.md` "Adding a migration" (register, bump
  `LATEST_VERSION` and `SCHEMA_VERSION`, test).
- `schema.sql` gains the same column so fresh databases match.

## Sync algorithm (per folder)

1. `select_readonly` (with CONDSTORE enabled when supported).
2. UIDVALIDITY check first; on mismatch invalidate and clear MODSEQ.
3. If stored `highest_modseq` is not null and server supports
   QRESYNC: QRESYNC discovery; fetch only returned new UIDs through
   the existing batch/`fetch_meta`/ingest path; apply VANISHED to
   `deleted_upstream`; store new `HIGHESTMODSEQ`.
4. Else: current UID-scan + full-listing reconcile path exactly as
   today; store `HIGHESTMODSEQ` opportunistically when the server
   provides it.
5. Commit watermarks (`last_seen_uid`, `highest_modseq`,
   `last_synced_at`) together per batch as today.

## Acceptance criteria

- [ ] Fallback parity: against a server without CONDSTORE, sync output
  (messages, facts, `deleted_upstream`, watermarks except MODSEQ) is
  identical before/after the change.
- [ ] Fast path: `FakeTransport` extended with MODSEQ/QRESYNC shows the
  QRESYNC path fetching only new/changed UIDs (assert on
  `meta_fetches`) while ingesting identical facts.
- [ ] Expunge parity: VANISHED-driven `deleted_upstream` marking equals
  full-listing reconcile results, including reappear un-marking.
- [ ] UIDVALIDITY mismatch clears `highest_modseq` to `NULL` and
  refetches, as with `last_seen_uid`.
- [ ] Read-only: test asserts no `STORE`/`EXPUNGE`/`MOVE` is ever issued
  on the QRESYNC path.
- [ ] Existing suites pass: `test_sync_incremental.py`,
  `test_deleted_upstream.py`, `test_determinism.py`,
  `test_privacy_invariants.py`, `test_migrations.py`, plus ruff format
  and check.
- [ ] Docs: `CHANGELOG.md` `[Unreleased]` entry, `docs/DESIGN.md`
  CONDSTORE section, `docs/TODO.md` checkbox update — same PR.

## Constraints (product ground rules)

- Nothing leaves the machine; no new network beyond the read-only IMAP
  session. No MODSEQ/UID data persisted beyond the `folders` watermark.
- No new persisted content: MODSEQ is a watermark, not message content.
  Privacy-invariants test must keep passing.
- Deterministic: same server state + same local state gives the same
  ingested facts regardless of path taken.
