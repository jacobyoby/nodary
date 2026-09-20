# ADR 0001: Unified API error envelope and paginated list responses

Date: 2026-09-20
Status: Accepted

## Context

Dashboard API was inconsistent: `GET /api/status?account=999` and `/api/messages?account=999` returned `{error:string}` 404, `/api/messages?tier=zzz` silently ignored the tier, `/api/messages?limit=abc` silently fell back to 200. List endpoints returned bare arrays; skill `api-and-interface-design` requires stable client contracts.

## Decision

- Introduce `_api_error(code, message, status)` → `{error, code, details?}` with consistent HTTP status (400/404). Keep `error` string for back-compat; add `code` (`account_not_found`, `invalid_tier`, `invalid_limit`, `sender_not_found`).
- Wrap list responses in `{data, pagination:{limit,returned,total?}}` per P0-3. UI `unwrap()` accepts both shapes during transition.
- Validate `tier` strictly 0..3, `limit` 1..1000 → 400.
- Typed as `ErrorResponse {error, code, details?}` and `Pagination` + `Paginated*` in `schemas.py`; `docs/specs/openapi.json` regenerated via `get_openapi_spec()` with CI drift guard.

## Consequences

- Clients handling bare arrays must unwrap `payload.data ?? payload`.
- Existing tests updated to unwrap and to expect 400 for invalid tier/limit.
- Wire keeps `snake_case` `anomaly_score` (documented deviation P2-1) for Python parity.

