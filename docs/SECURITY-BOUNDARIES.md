# Security Boundaries — nodary

Local-first, read-only analysis. This document states the isolation invariants the code must preserve (P0-4).

## Trust boundary

- **Process:** `nodary sync` and `nodary ui` run as the local user. Dashboard binds `127.0.0.1` only (never `0.0.0.0`). No cloud scoring, no telemetry.
- **Network:** No outbound requests after install. `tldextract` uses vendored PSL snapshot (`suffix_list_urls=()`), confusables from `data/uts39/confusables.txt`; `auth/oauth2.py` allowlists only Google + Microsoft token endpoints.
- **TLS:** `ui/tls.py` generates mkcert local CA cert; fallback plain HTTP warns. No secrets in DB.

## Data projection allowlist

`ui/server.py:_SCORED_MESSAGE_COLS` is the ONLY projection for scored rows. It explicitly excludes subject, body, filenames, full URLs. The dashboard is self-contained HTML (`src/nodary/ui/templates/index.html` — 605 lines, inline style+script, `tests/test_ui.py` asserts no `http://` `src=`).

```sql
_SCORED_MESSAGE_COLS = "m.id, m.message_id, m.sender_id, m.from_email_norm,
  m.from_display_name, m.sent_at, m.n_attachments, m.n_links, m.size_bytes,
  sc.anomaly_score, COALESCE(p.trust_tier, sc.trust_tier_at_scoring) AS tier, ..."
-- No subject, body, filename, full URL columns ever selected
```

Violations: adding any of those columns to the projection is a security bug.

## Untrusted data handling

- **Display names, link domains, attachment types** are untrusted mailbox content. Rendered only via `esc()` with `&<>"'` escaping (index.html:264) and never via `innerHTML` without escaping. `unwrap()` + `fetchJson` wrapper logs via `console.debug` without executing.
- **Message-ID in `message://` deep link** is `encodeURIComponent`'d and validated to exist before rendering; empty ID produces no link.
- **Skipped messages** store only `path/reason` (`transport.py:_failure_reason` returns type name, not payload).

## Secrets

- DB key + OAuth tokens via OS keychain (`storage/keys.py` `SERVICE=nodary`), `NODARY_DB_KEY` only for tests/CI. `schema.sql` has `CHECK(auth_method)` and no secret columns, `encryption=none` recorded when sqlcipher absent.
- `accounts.last_error` is a short string, never a stack trace with payload.

## Profile isolation

- `127.0.0.1` + `Host: localhost` only; no CORS, no external scripts, no outbound fetch. Chrome DevTools probes allowed only to `http://127.0.0.1:8321` via `.mcp.json` allowlist.
