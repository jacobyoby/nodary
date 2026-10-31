# ADR 0003: `nodary needs-me` command

Date: 2026-10-30
Status: Accepted

## Context

Scores answer "is this message suspicious?", not "does this message need the
user?". The trust tiers already carry the second signal: Tier 3 is
established two-way correspondence. A consumer outside the dashboard (a
Claude Code pane, a Mail smart mailbox) needs that view without re-deriving
tiers or opening the database itself.

## Decision

- Add `nodary needs-me [--days N] [--min-score S]`, printing a JSON array of
  incoming messages from the last N days (default 30) that come from a Tier 3
  sender or score at least S (default 25).
- Rows use the dashboard's `_SCORED_MESSAGE_COLS` projection and
  `_scored_payload`, so no subject, body, filename or full URL is exposed.
- Leave out mail from the user's own addresses (`user_identities`) and
  server-deleted messages.
- Leave out no-reply senders (`no-reply`, `donotreply`, `mailer-daemon`,
  `bounce`, ...) unless they reach the score threshold: replying once in a
  thread such a sender started can lift it to Tier 3, yet it cannot be a
  correspondent.

## Consequences

- The no-reply rule is a local-part pattern, not a scoring feature, so it
  does not change stored scores or `ENGINE_VERSION`.
- Senders the user replied to once (e.g. a retailer's order address) still
  appear; tier computation is unchanged.
