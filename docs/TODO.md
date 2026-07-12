# TODO / Roadmap

## v1 hardening (before first real-mailbox run)
- [ ] OAuth2 refresh-token flow for Gmail/M365 (currently: manually supplied
      access token via `nodary set-secret`; app passwords work end-to-end)
- [ ] Large-mailbox pass: measure a 100k-message backfill, tune BATCH_SIZE,
      consider fetching text parts only for messages ≤ N days old
- [ ] `nodary status` command (per-folder high-water marks, last sync, engine
      version, encryption state)
- [ ] Package a vendored copy of the PSL snapshot version in schema_meta and
      surface drift in the UI
- [ ] Handle IMAP CONDSTORE/QRESYNC where available (cheaper than UID ranges)
- [ ] Message deletion reconciliation (UIDs vanishing server-side; v1 keeps
      the local fact row, which is correct for baselines but should be marked)

## v1.x quality
- [ ] UI redesign — current dashboard is a bare-bones monospace list.
      Proper visual design: typography, light mode, sender avatars/initials,
      readable date grouping, score visualization better than a raw number.
      Constraint stays: single self-contained page, zero external assets.
- [ ] TLS for the dashboard. Note: Let's Encrypt can't issue for
      127.0.0.1/localhost, and requesting a public cert would also publish the
      hostname in Certificate Transparency logs — wrong tool for a
      privacy-first local app. Plan: locally-trusted cert via mkcert or a
      self-signed cert with a first-run trust prompt. If remote access is ever
      wanted, do it via Tailscale/SSH tunnel — never a public listener.
- [ ] Weight calibration harness: replay a labeled mailbox, report
      score distributions per tier (still fully local)
- [ ] Confusables table: replace curated subset with generated UTS #39
      skeleton data (vendored, versioned)
- [ ] Dashboard: sender drill-down page (baseline histograms, feature history)
- [ ] Dormant-resurrection: precompute median gap into sender_profiles to
      avoid the on-demand query entirely

## v2 (each stays local; see README threat model)
- [ ] Local body analysis phase (on-device only, opt-in, still no cloud)
- [ ] Multiple accounts in one dashboard
- [ ] Optional IMAP IDLE for near-real-time scoring
- [ ] Export/import of the encrypted profile db for machine migration
      (explicitly NOT sync between installs)

## Explicitly rejected (do not add)
- Telemetry of any kind, including "anonymous" usage stats
- Cloud scoring APIs, shared reputation feeds, cross-install score sharing
- Auto-delete / auto-move / auto-report actions
