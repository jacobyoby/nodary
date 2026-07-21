# nodary

Nodary is a local-first email heuristic analysis client. It connects to IMAP
read-only and builds a behavioral profile of senders from message metadata and
structure. All extraction, profiling, scoring, and display happen on the local
machine. Nodary has no telemetry and makes no cloud or analysis-service calls.

Scores are deterministic weighted sums of named features. They describe how a
message differs from known identity and behavior patterns; they are not a claim
that a message is malicious.

## Threat model

The behavioral profile is sensitive personally identifiable information. It
reveals correspondents, relationships, routines, and communication patterns.
It must never leave the machine.

Nodary is designed to surface evidence relevant to these attacks:

- Lookalike-domain phishing that resembles an organization the user trusts.
- Display-name impersonation of a trusted contact from a different address.
- A compromised contact whose messages shift away from their established
  behavior, such as a new payload type, link destination, sending hour, or
  message shape.
- Cold outreach carrying links, attachments, or a redirecting Reply-To address.

Nodary does not treat familiarity as proof of safety. Identity signals apply at
all trust levels, and a trusted contact can still score highly when their
behavior changes.

## Scoring features

The registry in `src/nodary/scoring/registry.py` is the authoritative list of
features and weights. Each feature produces a value from 0 to 1, and contributes
`value * weight` points. The total is capped at 100. Features are monotone: a
normal-looking signal cannot subtract points from another warning.

Identity and spoofing features apply to all tiers:

| Feature | Weight | Why it exists |
|---|---:|---|
| `lookalike_domain` | 25 | Detects registrable domains that resemble a trusted non-freemail domain through a Unicode confusable skeleton or small edit distance. This targets lookalike-domain phishing. |
| `display_name_collision` | 25 | Detects a display name matching a Tier 3 contact when the address differs. This targets direct impersonation of trusted people. |
| `auth_fail` | 15 | Uses receiving-server SPF, DKIM, and DMARC results as evidence that the sender identity may be forged. |
| `reply_to_divergence` | 10 | Flags an established sender redirecting replies to a different domain or an address not previously used. This catches account or identity abuse that moves the conversation elsewhere. |
| `embedded_addr_mismatch` | 10 | Flags an email address written in the display name when its domain differs from the actual From domain. This catches attempts to present a trusted address while sending from another one. |

Behavioral features apply only to Tier 2 and Tier 3 senders with at least eight
prior messages. Novelty signals use the confidence factor `n / (n + 10)`, so a
larger history provides stronger evidence.

| Feature | Weight | Why it exists |
|---|---:|---|
| `attachment_type_novelty` | 15 | Detects an extension/MIME pair never before seen from the sender. A new payload type is useful evidence of compromised-contact behavior shift. |
| `first_attachment_ever` | 10 | Detects the first attachment from a sender whose history had none. This is a broader payload-shift signal; the engine avoids double-counting it with attachment-type novelty. |
| `link_domain_novelty` | 10 | Measures the share of linked domains the sender has never used before. This targets compromised contacts introducing unfamiliar destinations. |
| `send_hour_anomaly` | 8 | Detects mail sent at an unusual hour in the sender's own clock from the Date header. This can expose a change in the person or system controlling the account. |
| `link_density_anomaly` | 5 | Detects substantially more links than the sender's baseline. This captures a shift toward link-heavy payload delivery. |
| `size_anomaly` | 5 | Detects message size far outside the sender's log-size baseline. Large or small structural changes can accompany a new payload or changed sending process. |
| `dormant_resurrection` | 5 | Adds context when a contact returns after at least 90 days and more than six times their typical gap, but only alongside another identity or behavioral flag. Dormant relationships are useful cover for compromised-contact attacks. |

Cold-contact features apply only to Tier 0 and Tier 1 senders. They provide
context for first or barely established contact; they do not by themselves
assert malicious intent.

| Feature | Weight | Why it exists |
|---|---:|---|
| `cold_attachment` | 12 | Highlights a payload from a never-seen sender, a common cold-outreach delivery pattern. |
| `cold_links` | 6 | Highlights links from a never-seen sender, where no behavioral history exists to judge the destinations. |
| `cold_replyto` | 8 | Highlights a never-seen sender redirecting replies away from the From identity, a useful sign of deceptive outreach. |

## Trust tiers

Tiers are computed, never manually assigned. Rules are evaluated top-down and
the first match wins:

| Tier | Rule |
|---:|---|
| 3 | At least one thread the user replied to, or at least one thread the user initiated: established two-way correspondence. |
| 2 | At least two messages spanning at least seven days, without a user reply: prior one-way contact. |
| 1 | The sender is new, but its registrable domain has at least one replied-to thread and is not a freemail domain: known organization. |
| 0 | Everything else: never seen or insufficient history. |

Freemail domains cannot confer Tier 1 on unrelated senders. Corresponding with
one Gmail address, for example, must not establish trust in every Gmail sender.

## Privacy invariants

- Body text is processed transiently only to extract HTTP(S) link hostnames and
  is discarded per message. Subjects are not retained.
- Filenames are not retained. Nodary keeps only attachment MIME type, derived
  extension, and size.
- Recipient lists are not retained. The documented exception is
  `message_recipients`: for outgoing mail only, it links a message to already
  known contact records. This is necessary to recognize user-initiated and
  replied correspondence; without it, Tier 3 could not be recomputed.
- Full URLs are not stored; only registrable link domains and counts are kept.
- IMAP credentials and OAuth tokens are stored in the OS keychain, not in the
  database.
- With the `sqlcipher` extra installed, the local database is encrypted with
  SQLCipher using a random 256-bit key held in the OS keychain. Without that
  extra, Nodary currently falls back to plain SQLite and prints a warning that
  the database is not encrypted at rest.
- Public Suffix List data and the freemail-domain list are vendored. Nodary does
  not fetch either list at runtime.
- The dashboard binds to `127.0.0.1` and its page has no outbound requests.
  It serves HTTPS via a locally-trusted mkcert certificate when available;
  certificate generation is fully local (no ACME, no Certificate
  Transparency log entries).

## Install

Nodary requires Python 3.12 or later and uses `uv` for the documented setup.

```sh
uv sync
```

For an encrypted database at rest, install the SQLCipher extra:

```sh
uv sync --extra sqlcipher
```

## CLI

Register an IMAP account. The command prompts for an app password or OAuth2
access token and stores it in the OS keychain.

```sh
uv run nodary add-account you@example.com --host imap.example.com
uv run nodary add-account you@example.com --host imap.example.com --auth oauth2
uv run nodary add-account you@example.com --host imap.example.com --alias alias@example.com
```

Update the keychain secret for an existing numeric account ID:

```sh
uv run nodary set-secret 1
```

Run an incremental read-only sync and score new mail:

```sh
uv run nodary sync
```

Recompute all derived profiles, tiers, and scores from locally stored facts:

```sh
uv run nodary rebuild
```

Start the local dashboard at `https://127.0.0.1:8321/`:

```sh
uv run nodary ui
uv run nodary ui --port 9000
uv run nodary ui --no-tls   # force plain HTTP
```

### Dashboard TLS

The dashboard serves HTTPS using a locally-trusted certificate generated by
[mkcert](https://github.com/FiloSottile/mkcert). Let's Encrypt is deliberately
not used: it cannot issue for `127.0.0.1`/`localhost`, and a public
certificate would publish a hostname in Certificate Transparency logs — the
wrong tool for a privacy-first local app. mkcert generates the certificate
entirely on this machine, signed by a CA that exists only on this machine.

One-time setup:

```sh
brew install mkcert
mkcert -install   # trust the local CA (asks for your password once)
```

On the next `nodary ui` start, a certificate for `localhost`, `127.0.0.1`,
and `::1` is generated into `~/.nodary/tls/` (override with
`NODARY_CERT_DIR`) and the dashboard serves HTTPS. If mkcert is not
installed, the dashboard falls back to plain HTTP on `127.0.0.1` with a
visible warning. If you skip `mkcert -install`, HTTPS still works but the
browser will warn about an untrusted certificate until you run it.

The dashboard still binds `127.0.0.1` only. For remote access use an SSH
tunnel or Tailscale — never a public listener.

### Incremental sync

For each folder, Nodary stores the server's `UIDVALIDITY` and a
`last_seen_uid` high-water mark. A normal sync requests only UIDs greater than
that mark and advances it after successful batches. If the server's
`UIDVALIDITY` changes, old facts for that folder are invalidated, the folder is
refetched, and profiles and scores are rebuilt. Sent mail is synced before
incoming mail so relationship evidence is available during scoring.

The IMAP transport uses non-mutating fetches. It never sets flags, moves,
copies, expunges, deletes, or sends mail.

## Non-goals for v1

- No NLP, semantic classification, or body-content analysis.
- No automatic delete, move, quarantine, or abuse reporting.
- No sharing or synchronization of scores or behavioral profiles between
  installations.
- Nodary never sends mail and never modifies mailbox contents.
