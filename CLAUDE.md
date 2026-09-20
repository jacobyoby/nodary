# CLAUDE — project context for AI agents

## What nodary is

Local-first email heuristic analysis client. Reads IMAP or Apple Mail store read-only, builds per-sender behavioral baselines, scores messages as weighted sum of named features (capped 100). No telemetry, no cloud scoring, no body/subject persistence.

## Privacy invariants (never change without ADR)

- Body text transient: extract `https://` hostnames then discard per message; `MAX_TEXT_SCAN_BYTES=1MiB`.
- Filenames/subjects/full URLs/recipient lists not persisted (exception: `message_recipients` for outgoing mail only).
- Attachments: only `MIME + extension + size` stored; no bytes.
- Secrets: `keyring` (`SERVICE=nodary`) + `NODARY_DB_KEY` test override; `encryption=sqlcipher|none` in `schema_meta`.

## Repo layout

- `src/nodary/pipeline.py` — ingest + `rebuild()` in `sent_at` order
- `src/nodary/feature_extraction/` — `records.py` (MessageRecord), `profiles.py` (PROFILE_VERSION=1), `normalize.py` (TLDExtract offline)
- `src/nodary/scoring/` — `registry.py` ENGINE_VERSION=1.1.0, `engine.py`, `tiers.py`
- `src/nodary/storage/` — `schema.sql` (source of truth), `db.py`, `migrations/_00x`, `keys.py`, `psl.py`
- `src/nodary/ui/` — `server.py` (127.0.0.1 Flask, 6 GET routes, _SCORED_MESSAGE_COLS), `schemas.py` (TypedDicts), `templates/index.html` (self-contained), `tls.py`
- `src/nodary/imap_sync/`, `mail_store/` — transports (read-only)
- `tests/` — 31 modules; `docs/DESIGN.md` authoritative spec; `docs/specs/` specs

## Commands

- `uv sync --locked` · `uv run pytest -q` · `uv run ruff check --output-format=github` · `uv run ruff format --check` · `ty check` (new)
- `uv run pytest -q --cov` for coverage; `nodary sync` / `nodary rebuild` / `nodary ui` for manual

## Skills & constraints

- Active skills: 25 from `/Users/jacobrakai/agent-skills` (see `~/.agents/skills`). Relevant: `api-and-interface-design`, `security-and-hardening`, `test-driven-development`, `constraint-driven-development`.
- Quality bar: `CONSTRAINTS.md` (lint, format, tests, type, coverage, migration idempotency, security). CI enforces.
- Decisions: `docs/decisions/` ADRs (create `NNNN-slug.md` per change).

## API contract

`schemas.py` → `docs/specs/openapi.json` → `server.py`. Envelope `{data, pagination:{limit,returned,total}}`, error `{error, code, details}`. Wire `snake_case` anomaly_score (documented deviation).
