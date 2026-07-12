## What & why
<!-- One concern per PR. Link the issue. -->

## Checklist
- [ ] `uv run pytest` passes
- [ ] `uv run ruff format . && uv run ruff check .` clean
- [ ] CHANGELOG.md updated under [Unreleased]
- [ ] No new outbound network traffic; no new persisted message content
      (or: documented in docs/DESIGN.md and called out below)
- [ ] Scoring changes: ENGINE_VERSION bumped, registry rationale added,
      synthetic-mailbox test asserting feature names + explanations
- [ ] New derived state: added to `pipeline._DERIVED_TABLES` and covered by
      the rebuild-determinism test

## Privacy notes
<!-- Anything a reviewer should double-check against the threat model. -->
