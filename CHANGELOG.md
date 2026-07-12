# Changelog

All notable changes to nodary are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The scoring engine has its own version
(`ENGINE_VERSION` in `src/nodary/scoring/registry.py`) recorded on every
stored score; bump it whenever a feature, weight, or threshold changes.

## [Unreleased]

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
