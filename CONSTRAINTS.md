# Constraints — nodary quality bar

Written contract per constraint-driven-development skill. The quality bar is enforced by CI; weakening it must not be done to “go green”.

## Thresholds

- **Lint:** `ruff check` must pass (`pyproject.toml [tool.ruff] select E,F,W,I,UP,B,SIM`, ignore SIM108,E501). No `@ts-ignore`/`eslint-disable`-style suppressions beyond `pyproject.toml` per-file-ignores and `sqlcipher3` `type:ignore`.
- **Format:** `ruff format --check` must pass.
- **Tests:** `pytest -q` must pass (currently 233 passed, 5 skipped). No `skip`/`xfail`/`TODO` to silence failures.
- **Type:** `ty check` (or `pyright`) gate — added this PR; intentional omission must be documented here with rationale. No new `Any` in public APIs.
- **Coverage:** ≥ 85% line coverage on `src/nodary` (future gate; currently measured via `.coverage`).
- **Migrations:** All migrations `LATEST_VERSION=6` must be idempotent (`IF NOT EXISTS`, `table_info` guards). `schema.sql` is source of truth, `migrations/__init__.py` replays.
- **Security:** No `gitleaks`/`semgrep` high, no hardcoded secrets, `storage/schema.sql` comment `tokens live in OS keychain` must remain.

## Watchlist (diff watchers)

- New `@ts-ignore`/`eslint-disable`/`noqa` beyond the two allowed suppressions → fail.
- Deleted/skipped tests or assertions → fail.
- `NotImplemented` stubs or `TODO`/`FIXME` in `src/` → fail.
- `unimplemented` or `empty catch` → fail.
- Lowering any threshold above → requires ADR in `docs/decisions/`.

## Pointers

`AGENTS.md` / `CLAUDE.md` enumerate this file. CI `.github/workflows/ci.yml` enforces it.
