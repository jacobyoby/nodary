# Contributing

## Ground rules (these are the product)

1. **Nothing leaves the machine.** No telemetry, no cloud calls, no external
   scoring APIs, no "anonymous" stats. The only network connection this
   codebase may open is the read-only IMAP session (and Flask bound to
   127.0.0.1). PRs adding any other outbound traffic will be rejected.
2. **No new persisted content.** Body text, subjects, filenames, and full
   URLs are never written to the database. If a feature needs new data,
   derive the minimal structural value (count, extension, registrable
   domain, histogram bucket) and document it in `docs/DESIGN.md`.
   `tests/test_privacy_invariants.py` fails CI if a new column or extract
   path starts keeping that content; see `SECURITY.md`.
3. **Scoring stays deterministic and explainable.** Every feature returns a
   normalized [0,1] raw value, a registry weight, and a rendered explanation
   string. No opaque models, no randomness, no wall-clock dependence in
   scoring paths. Same message + same profile state must produce the same
   score bit-for-bit — `tests/test_determinism.py` enforces this.
4. **Derived state must be rebuildable.** Anything written outside the fact
   tables (`messages`, `message_attachments`, `message_link_domains`,
   `message_recipients`, `threads`, `folders`, `senders` identity columns)
   must be regenerated exactly by `nodary rebuild`. If you add derived
   state, add it to `pipeline._DERIVED_TABLES` and extend the replay.
5. **Registry discipline.** New/changed features, weights, or thresholds go
   in `scoring/registry.py` with a rationale docstring, a CHANGELOG entry,
   and an `ENGINE_VERSION` bump.

## Workflow

- Python 3.12+, `uv sync` to set up, `uv run pytest` must pass.
- Confusables updates: pin a new UTS #39 drop under `third_party/unicode/uts39/`
  and regenerate with `uv run python scripts/generate_confusables.py` (see
  `docs/DESIGN.md` Normalization). Do not fetch Unicode data at runtime.
- Format & lint: `uv run ruff format . && uv run ruff check .` before pushing
  (config lives in `pyproject.toml`; CI treats warnings as errors).
- New scoring behavior needs a synthetic-mailbox test in `tests/` using the
  `Mailbox` fixture — assert on feature names and explanation text, not just
  the total score.
- Update `CHANGELOG.md` under `[Unreleased]` in the same PR.
- Use the PR template; keep PRs to one concern.

## Style

- Standard library first; every new dependency needs a stated reason in the
  PR (and must not fetch anything at runtime — see the vendored PSL rule).
- Comments explain constraints ("bounded because 100k mailboxes"), not
  mechanics.
- SQL lives next to the code that owns it; no ORM.

## Schema migrations

Schema changes that cannot be expressed by `CREATE TABLE IF NOT EXISTS`
(new columns, widened CHECK constraints, new indexes on existing columns)
go in `src/nodary/storage/migrations/`. See the "Schema Migrations"
section in `docs/DESIGN.md` for the full pattern. Every migration must
be idempotent and have a test.
