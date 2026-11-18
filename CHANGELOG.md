# Changelog

## 4.2.2 — 2026-11-18

### Fixed

- v4 decision evidence now excludes the versioned `decision_format_contract`
  before calling the strict market-timestamp and stock-series binders. The
  known contract remains in the model prompt, while the lower-level evidence
  functions continue to reject unknown assessment fields.

### Verification

- Python: 3,817 passed, 7 skipped.
- Required Node governance suite: 188 passed.

## 4.2.1 — 2026-11-17

### Fixed

- SEC `robots.txt` requests now use the same admitted operator User-Agent as
  SEC submissions and archive documents, including the robots product-token
  match. Non-SEC hosts continue to receive the generic Tiger research agent and
  never receive the operator email.
- Same-process `WorkStore` writers serialize short SQLite transactions before
  entering SQLite, preventing high-capacity coordinator runs from exhausting the
  cross-process busy retry window on slow CI runners.

## 4.2.0 — 2026-11-17

### Added

- `us-tech-intelligence-paper-v4` and
  `tiger.public-tech-intelligence-paper-policy.v4`; v3 prompt, hash and receipt
  semantics remain frozen for history.
- `tiger.run-readiness/v1`, `tiger.process-observation/v1`,
  `tiger.attempt-quarantine/v1`, `tiger.validation-diagnostic/v1`,
  `tiger.run-progress/v1` and `decision-format-v1`.
- Append-only source/model quarantine for unknown attempts. Reservations and
  unknown usage remain disclosed and are never silently freed or re-sent.
- Atomic progress projection for market batches and provider-native batch
  progress.
- Hash-locked `requirements.lock` and backend/full setup profiles.

### Fixed

- Config generation now accepts only reviewed Tiger identities and rejects
  role, bundle, repository or MCP-entry drift.
- macOS process observation uses `libproc` rather than `/bin/ps`.
- Model completion equal to the exact output ceiling is exhausted, not success.
- Document-level schema failures are local in v4 and do not consume the global
  format reanswer.
- Provider batches share one hard deadline, preserve completed symbols and
  disclose timed-out symbols.
- Final buy/watch citations use exactly their own market ID; BUY additionally
  requires owned frozen company evidence.

### Preserved

- MRS / SHS / ICS / TSS / L4 thresholds and 1R/20% risk limits.
- Public-only data, paper account, append-only evidence and no broker orders.
- v3 and earlier profiles for historical replay.

## 4.1.0 — 2026-11-13

TigerTest is a complete isolated copy of
`geniusdapeng-collab/workroom-tiger@d833064d072cc747b2004bc909e2cb4f3d18df36`.

### Added

- New `us-tech-intelligence-paper-v3` profile and
  `tiger.public-tech-intelligence-paper-policy.v3`.
- `tiger.tech-focus.v1` domain pack covering semiconductor-cycle and
  AI-infrastructure themes.
- 79-name reviewed US-listed technology universe derived from `semis` and
  `ai_compute`.
- Focused hard filters: USD 10 minimum price, USD 100M ADV20, 260-day history
  and 12% ATR ceiling.
- A maximum 16-name frozen research frontier with at least four names reserved
  for each parent theme (account holdings/pending consume frontier slots).
- Deeper research graph: global macro, six technology subchains, XLK/SMH and
  three evidence questions per frontier security.
- A bounded 180-second batch-market fallback budget with 20-symbol chunks.
- `tiger.decision-surface.v1`: actionable plans, conditional observations,
  setup-pending names, preliminary candidates, research gaps and reopen
  conditions.
- Markdown and HTML rendering for the two-theme decision surface.
- `docs/TIGERTEST-ARCHITECTURE.md` with alternatives, failure paths, migration
  and acceptance metrics.

### Preserved

- MRS / SHS / ICS / TSS / L4 thresholds and 1R/20% risk limits.
- Full 21-stage redline, source evidence, frozen acquisitions and independent
  receipt verification.
- Historical `us-public-intelligence-paper-v2` and earlier profiles.
- Paper-only boundary and prohibition on real broker orders.

### Verification

- Python: 3,754 passed, 7 skipped.
- Real public-data smoke with Tencent: 79/79 market frames, 79 scanned, 73
  passed hard filters, 16 researched candidates, 0 actionable plans and 8
  conditional observations in 216.6 seconds.
- Offline end-to-end focus demo: one structure-valid paper plan and eight named
  conditional/setup-pending observations.
