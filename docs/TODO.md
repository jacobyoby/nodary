# Roadmap

Nodary is currently **v0.3.1**. This file tracks shipped capability and open
work; `CHANGELOG.md` remains the release-history source of truth.

## Done by v0.3.1

- [x] Read-only incremental IMAP sync with UIDVALIDITY/high-water-mark tracking.
      Why: gives deterministic local history without mutating the mailbox.
- [x] Header, BODYSTRUCTURE, bounded text-part extraction, and attachment/link
      structure parsing without persisting body text, subjects, filenames, or
      full URLs. Why: keeps the behavioral profile privacy-preserving.
- [x] Sender/domain profiles, trust tiers 0-3, and deterministic scoring engine
      v1.0.0 with stored feature explanations. Why: scores are recomputable and
      inspectable.
- [x] `nodary rebuild` replay path. Why: profiles, tiers, and scores can be
      regenerated from fact tables after backfills or schema/profile changes.
- [x] SQLCipher-at-rest support keyed from the OS keychain, plus visible
      plain-SQLite fallback for development. Why: mailbox-derived behavioral
      profiles are sensitive local data.
- [x] Localhost-only dashboard with tier filters and per-message score
      decomposition. Why: review happens locally with no outbound requests.
- [x] Dashboard redesign and TLS via local mkcert (v0.2.0). Why: the UI is
      more readable, and local HTTPS avoids public certificates or CT logs.
- [x] Apple Mail store transport and `nodary set-source <id> imap|mail-store`
      (v0.3.0). Why: supports accounts where direct IMAP is unavailable, while
      remaining read-only.
- [x] CI for lint, format check, tests, and SQLCipher-extra coverage (v0.3.0).
      Why: catches lockfile, formatting, interpreter, and optional-encryption
      regressions before release.
- [x] `accounts.auth_method` supports `mail_store` for new databases and has an
      in-place widening migration for older databases. Why: source switching
      should not require recreating the local database.
- [x] Weight calibration harness that replays a labeled mailbox and reports
      score distributions per tier. Why: deterministic weights now have an
      offline empirical harness using synthetic benign and malicious scenarios.
- [x] `nodary status` command showing per-folder high-water marks, last sync,
      engine version, and encryption state (v0.3.1). Why: operators can inspect
      sync health from the CLI without opening the dashboard.

## Near-Term / Hardening

- [x] OAuth2 refresh-token flow for Gmail/M365. Why: current OAuth2 support
      stores a manually supplied access token and requires `nodary set-secret`
      when it expires. Shipped in #46 / on main.
- [x] Large-mailbox pass: measure a 100k-message backfill, tune
      `imap_sync.sync.BATCH_SIZE`, and consider fetching text parts only for
      messages <= N days old. Why: first-run performance and memory behavior
      need real-mailbox validation. Shipped in #47 / on main.
- [x] Package a vendored Public Suffix List snapshot version in `schema_meta`
      and surface drift in the UI. Why: registrable-domain decisions affect
      scoring and should be auditable across rebuilds. Shipped in #46 / on main.
- [x] Generalize schema migrations beyond the current `accounts.auth_method`
      rebuild. Why: `schema_meta.schema_version` exists, and future table or
      column changes need a reliable upgrade path for existing databases.
      Shipped in #46 / on main.
- [x] Message deletion reconciliation for UIDs that vanish server-side. Why:
      retaining local facts is correct for baselines, but deleted/server-missing
      messages should be marked so sync status is understandable.
      Shipped in #46 / on main.
- [x] Mail-store portability checks for Apple Mail layouts beyond verified V10.
      Why: `mail_store.store` currently assumes the V10 layout and should fail
      clearly or support newer layouts when macOS changes them.
      Shipped in #46 / on main.

## Mid-Term

- [ ] Handle IMAP CONDSTORE/QRESYNC where available. Why: change-aware sync is
      cheaper than UID range scans for large mailboxes.
- [ ] Confusables table generation from vendored Unicode UTS #39 skeleton data.
      Why: the current curated subset is auditable but incomplete.
      Partial on main (generated table + vendored `data/uts39`); held #39 is an
      alternate expansion — leave open until Jacob/CoS delta call.
- [x] Dashboard sender drill-down page with baseline histograms and feature
      history. Why: a high score is easier to trust when the underlying sender
      baseline is visible.
- [ ] Dormant-resurrection median-gap precomputation in `sender_profiles`. Why:
      the scoring engine currently queries message history on demand for that
      rare feature.
- [x] Multiple accounts in one dashboard. Why: storage supports accounts, but
      the dashboard is not yet account-aware for filtering and comparison.
      Shipped in #46 / on main (account switcher; dashboard sync-health strip
      also landed in #46).
- [ ] Optional IMAP IDLE for near-real-time scoring. Why: polling is enough for
      v1, but IDLE can reduce latency without changing the local-only model.
- [ ] Product decision on install-global vs account-scoped sender/domain
      baselines for multi-mailbox installs. Why: work+personal blend is
      current schema behavior and needs an explicit stance before schema
      changes.

## Later / Research

- [ ] Local body analysis phase, on-device only and opt-in. Why: semantic or
      content signals may help, but they must not weaken the privacy model.
- [x] Export/import of the encrypted profile database for machine migration.
      Why: moving a local-first installation should be explicit and should not
      become cross-install sync. Shipped in #46 / on main.

## Explicitly Rejected

- Telemetry of any kind, including "anonymous" usage stats.
- Cloud scoring APIs, shared reputation feeds, or cross-install score sharing.
- Auto-delete, auto-move, auto-report, quarantine, or any other mailbox action.
- Multi-user / household sharing, org admin consoles, or pushed policy CDNs
  that distribute rules or reputation across installs.
- Cross-install sync of profiles, scores, or policy (beyond explicit
  user-driven `export-profile` / `import-profile`).
