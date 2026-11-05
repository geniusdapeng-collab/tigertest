# Tiger PnL public ledger

Tiger PnL means **Tiger Profit & Loss｜投资业绩与决策记录**. This independent website publishes genuine account snapshots and model-generated Tiger trading instructions for a human execution team. The team executes the instructions at its brokers; actual fills, positions and NAV are recorded from execution reports. The website itself has no broker order endpoint. An empty deployment displays `awaiting_first_record`; it never seeds illustrative performance.

## Runtime and private storage

No npm packages or build step are required. Run with the existing Node 24 runtime. The service listens on `127.0.0.1:4348`; put HTTPS Nginx in front of it. `pnl/public` contains the static frontend. Configure these values in the server's protected service environment, outside Git:

| Variable | Meaning |
|---|---|
| `TIGER_PNL_WRITE_TOKEN` | Shared Bearer secret, 32–512 characters, no whitespace. Generate a random secret in the deployment's protected secret store. |
| `TIGER_PNL_DATA_DIR` | Required absolute private directory outside the source tree; only the service OS account should have access. |
| `PORT` | Optional port, defaults to `4348`; the bind address is always loopback. |

Start with `node pnl/server.mjs`. Run tests with `node --test pnl/test/*.test.mjs`. Do not put credentials or real snapshot files in the repository, shell history, public assets or test fixtures. Public read endpoints require no credentials. The write token belongs only in an AI runner's secret environment and the service's protected environment.

The files `DATA_DIR/records/000000000001.json`, etc. form a sequential SHA-256 chain. Writes are serialized in a single process, written into an exclusive temporary file, fsynced, atomically hard-linked to a previously nonexistent final filename, and directory-fsynced before returning a receipt. Existing accepted files are never replaced. A write error stops further writes until recovery. Startup reads and validates every accepted record, sequence, payload hash and chain hash. A process owner lock prevents concurrent ordinary starts; only a provably dead local PID's stale lock is reclaimed. The lock is a startup guard, not a cross-process atomic mutex: simultaneous stale-lock recovery can race. Production must run one systemd service instance under an outer OS lock (`/usr/bin/flock`) and use a local filesystem, not NFS or shared multi-host storage. Data paths are resolved through symlinks and rejected if their real target is inside the source or public static directory.

An interrupted write may have committed before the network response was received. Always retry with **the same key and exact same payload**. A leftover uncommitted `.pending-*` file is removed on verified startup. A committed record survives a restart. An unreadable process lock or damaged chain fails closed and needs operator review. Back up the entire data directory regularly; hashes detect edits to chain contents but do not prove broker authenticity or prevent an administrator from replacing the entire ledger or truncating its tail. Archive receipts independently if independent tamper evidence is needed.

## Public API contract

`GET /healthz` → `{ "status": "ok", "records": 0, "research": 0, "ledger": "verified" }`. A failed durable write in either ledger changes health status to `503/unavailable`.

`GET /api/public/dashboard` always returns this shape:

```json
{
  "schemaVersion": 1,
  "status": "awaiting_first_record",
  "generatedAt": "ISO timestamp",
  "baseline": null,
  "latest": null,
  "metrics": null,
  "history": [],
  "records": [],
  "research": { "latest": null, "history": [] }
}
```

After the first genuine record, `status` becomes `active`, with these exact field definitions:

| Field | Structure |
|---|---|
| `baseline` | `{date: asOf-of-first-record, nav: first-account-nav}` |
| `latest` | Public snapshot, described below |
| `metrics` | `{returnPct, drawdownPct, unrealizedPnl, positionCount, recordCount, netExternalFlow}` |
| `history` | All actual observations in chronological order: `[{asOf, nav, returnPct, drawdownPct}]` |
| `records` | Up to 20 latest public snapshots, newest first |
| `research` | Independent research `{latest:null\|public-research,history:[public-research]}`; all actual research entries newest first, does not affect account `status`, baseline or history |

A public snapshot is `{sequence, hash, asOf, account:{currency,nav,cash}, positions, trades, decisions}`. Each position is `{symbol,name,quantity,avgCost,price,marketValue,weight,unrealizedPnl}`. `weight` is a fraction (0–1); `unrealizedPnl` is USD. Trades and decisions use the exact submission fields listed below. **Account label, idempotency key, provenance, evidence hash, source reference and attestor are excluded from every public projection.** Public strings are plain text and must be rendered with DOM `textContent`, never inserted as HTML.

`GET /api/public/records?limit=20&offset=0` → `{schemaVersion:1,total,limit,offset,records:[public-snapshots]}`, newest first. `limit` is 1–100; `offset` is a nonnegative safe integer. Unknown or repeated pagination parameters return `400`.

## Return methodology

The first record starts the public account: its return and drawdown are **0%**, with no invented earlier history. For each subsequent snapshot, multiply the previous unit index by `(current NAV − net external flow) / previous NAV`. Deposits are positive and withdrawals negative. The submitted flow is assumed to occur at the end of the observation interval. Report `returnPct = (index − 1) × 100`; `drawdownPct = (index / highest-observed-index − 1) × 100`, expressed as a nonpositive percentage. The metric is the current drawdown, not maximum historical drawdown. This end-of-period flow convention is approximate when substantial cash movements happen during the interval; submit an extra snapshot immediately before and after a cash movement for better attribution.

The first record must have `netExternalFlow:0`, because its NAV establishes the starting capital. Cumulative `metrics.netExternalFlow` sums subsequent flows only. NAV must equal cash plus long-only position market values; account label is fixed after baseline. Leveraged, short, multi-currency and derivatives accounts are deliberately outside this schema. No annualized return, backfilled simulation curve or benchmark comparison is fabricated. Multiple genuine observations per day are supported and are retained as actual observations.

## Authenticated submission

`POST /api/v1/records` with `Authorization: Bearer <secret>` and `Content-Type: application/json`. The JSON body below describes the schema, **not a real submission**; never publish placeholders. All fields are required and unknown fields are rejected at every object level.

| Field | Required structure / constraints |
|---|---|
| `schemaVersion` | `1` |
| `idempotencyKey` | Unique 8–128 characters matching `[A-Za-z0-9][A-Za-z0-9_.:-]*`; persist it before submitting. |
| `asOf` | Actual account valuation time in ISO format with explicit timezone, strictly later than the last accepted record; future times over five minutes are rejected. |
| `account` | `{label:string,currency:"USD",nav:number,cash:number}`; private stable account alias, never a broker account number; positive NAV and nonnegative cash. |
| `positions` | `[{symbol,name,quantity,avgCost,price,marketValue}]`, up to 500 unique symbols; quantity/price/value positive, average cost nonnegative; value must equal quantity × price within rounding tolerance. Empty means genuinely all cash. |
| `trades` | `[{id,executedAt,symbol,side:"buy"\|"sell",quantity,price,fees}]`, up to 2,000. Only newly disclosed actual executions since the previous snapshot; IDs may never repeat. Empty means no new confirmed executions. |
| `decisions` | `[{id,symbol,action:"buy"\|"sell"\|"hold"\|"watch",targetWeight,thesis,decidedAt,runId}]`, up to 500, target fractions 0–1 with sum ≤1. Research recommendations are distinct from actual executions and holdings. |
| `netExternalFlow` | Signed USD deposits minus withdrawals during this snapshot's interval; first record must be zero. |
| `provenance` | `{kind:"broker_statement"\|"operator_attested",evidenceSha256,sourceRef,attestedBy}`; real evidence hash (64 lower-case hexadecimal characters), private evidence reference and responsible attestor. No placeholders, paper, mock, simulation or synthetic sources. |

An optional `Idempotency-Key` header must match the body's key. Newly committed submission returns HTTP `201` with `{receipt:{schemaVersion,idempotencyKey,sequence,asOf,acceptedAt,hash,previousHash,payloadHash,replayed:false}}`. Retrying the same payload returns `200` with `replayed:true`; reusing a key for changed content returns `409/IDEMPOTENCY_CONFLICT`. Corrections require a **new later snapshot** and a new key; there is no delete or overwrite API.

`GET /api/v1/receipts/:idempotencyKey` with the same Bearer authentication returns `{receipt}` or `404/RECEIPT_NOT_FOUND`. This endpoint returns no account or evidence details.

`operator_attested` accepts an explicit truthful account-owner/operator confirmation, including a genuinely all-cash starting account. Hash the exact confirmation text as evidence and keep its reference private. The API validates the evidence form and declared source; it cannot independently verify whether a token holder's assertion is truthful. The operator must inspect real records before attesting. A successful Tiger paper workflow is research evidence only and **must never be used as the source of real NAV, real holdings or real executions**. Research decisions may refer to its verified `runId`, while actual account facts come from real evidence.

Errors use `{error:{code,message}}`. Expected codes include `UNAUTHORIZED` (401), `JSON_REQUIRED` / `ENCODING_NOT_SUPPORTED` (415), `BODY_TOO_LARGE` (413), `INVALID_JSON` (400), `INVALID_RECORD` / `REAL_EVIDENCE_REQUIRED` / `BASELINE_FLOW_MUST_BE_ZERO` / `INVALID_EXTERNAL_FLOW` / `TRADE_OUTSIDE_PERIOD` (422), `IDEMPOTENCY_CONFLICT` / `AS_OF_NOT_INCREASING` / `ACCOUNT_MISMATCH` / `DUPLICATE_TRADE` (409), and `LEDGER_UNAVAILABLE` / `LEDGER_WRITE_FAILED` (503). Bodies are limited to 1 MiB; header/request/socket timeouts are explicit. No request body, authentication header or token is logged.

## AI publisher workflow

1. Run Tiger via its controlled agent runner. Obtain its independently verified receipt and use its verified research decisions only.
2. Obtain genuine account-owner confirmation or broker/operator evidence of NAV, cash, actual positions and newly executed trades. Preserve evidence privately and calculate its SHA-256. Do not infer a fill from a recommendation.
3. Prepare a complete JSON snapshot in a private path outside the repository, with one stable idempotency key. Include verified research in `decisions`; keep actual holdings and executions evidence-based.
4. The AI runner receives `TIGER_PNL_WRITE_TOKEN` from its secret environment and optionally `TIGER_PNL_URL` (defaults to `https://tigerpnl.com`). Run `node pnl/publish.mjs /private/path/real-snapshot.json`.
5. The CLI submits via HTTPS with redirects disabled, fetches the authenticated receipt, and checks it against the local payload hash. It prints only the receipt. Network failure is not proof that submission failed; recover by querying/retrying the original key and payload.

## Independent periodic research

Daily Tiger research can update while the real account snapshot remains unchanged. Submit research to `POST /api/v1/research` with the same Bearer token, JSON content type, idempotency rules and 1 MiB limit. Its independent durable chain is stored under `DATA_DIR/research/records`; account records remain under `DATA_DIR/records`. Query authenticated research receipts at `GET /api/v1/research/receipts/:idempotencyKey`. Publish with `node pnl/publish.mjs --research /private/path/verified-research.json`; the default account command is unchanged.

New submissions require **schemaVersion 2**, the `us-public-model-required-paper-v1` run profile and a fully completed real DeepSeek assessment. Historical v1 records keep their original bytes, hashes and disclosed model mode, and remain readable and recoverable. Retrying the identical historical key cannot create another record. New disabled, passthrough or legacy-profile publications are rejected with `MODEL_REQUIRED`.

| Field | Meaning |
|---|---|
| `schemaVersion`, `idempotencyKey`, `asOf` | Version `2`, stable safe key, original sealed run completion timestamp; increasing within the decision ledger. |
| `runId`, `receiptHash`, `dataAsOf` | Independently verified run, exact original receipt SHA-256 and actual benchmark last-bar date. A run is published once. |
| `executionMode` | `paper`: the controlled calculation/accounting environment. The published model decisions are instructions for the human execution team; the paper ledger is never copied into actual fill or NAV fields. |
| `modelMode`, `status` | Exactly `enabled` and `completed`. A failed or degraded model cannot be published as a new successful instruction. |
| `universeSize`, `summary`, `decisions` | Verified scan count, model-written plain-language overall action, and compatibility decision list. Every decision refers to this run. No score/formula string is substituted for a model explanation. |
| `model` | Exact fields: `provider`, `model`, `release`, `required`, `response_ids`, `call_count`, `required_stages`, `stage_counts`, `search_count`, `search_mode`, `all_required_completed`. Provider `deepseek`, API ID `deepseek-flash`, observed release `DeepSeek-V4.1-Flash-1017`, required/completed booleans true, native search mode `provider_native_search`. Each of the five stages must have at least one actual response and unique provider ID; counts reconcile. |
| `executionDecisions` | Exact snake-case fields per card: `symbol`, `action`, `target_weight`, `action_summary`, `why_this`, `why_now`, `entry_condition`, `invalidation`, `risk_note`, `sources`, `market_evidence`, `entry_price`, `stop_price`, `shares`, `risk_usd`, `time_stop_days`, `valid_for`, `reference_capital_usd`. Text comes from the model; numeric plan facts come from the existing validated risk plan. `valid_for` is an ISO timestamp with timezone. |

Every required-model instruction is `buy` or `watch`. Each includes exactly one own-stock `market_evidence` entry with exactly `{id,symbol,as_of,close,series_sha256}`. The runtime derives the actual last stock-bar date, finite positive last close and SHA-256 of the full canonical normalized frozen bar series. Its ID is `md_<symbol>_<YYYYMMDD>_<first-24-series-hash-characters>`; the date cannot exceed the frozen New York assessment date. Website validation checks this identity, calendar date and public numeric/hash shape against the New York date of research `asOf`. It does not use the benchmark date as the stock-date ceiling, because valid stock and benchmark series can have different last dates. It cannot independently recalculate a private series from the public hash; controlled receipt verification does that before publication. No provider URL or provider label is invented for the snapshot. The UI shows its date and close separately from news.

News `sources` contain zero to eight `{id,title,url,published_at}` entries for a watch, and one to eight for a buy. News IDs cannot use the reserved `md_` prefix. The model must cite its own market snapshot in `source_ids`; a watch can cite that snapshot without unrelated news. A buy must also cite at least one provided native-search article. Links must be public HTTP(S), without credentials, fragments or credential query keys. `published_at` is a parseable provider publication time or null when unavailable, not an independent publication-date attestation. Buy cards require positive entry, size, stop below entry, risk budget and reference capital. Watch entries must have target zero and exactly null `entry_price`, `stop_price`, `shares`, `risk_usd` and `time_stop_days`; the actual closing reference remains in `market_evidence`, and reference capital remains separate. Null execution values never become invented zero-price orders. Instruction symbols/actions/weights exactly match `decisions`. This extended instruction contract does not add sell or hold output to the required model; legacy and actual-account decision contracts retain their existing actions.

The public decision shape is `{schemaVersion,sequence,hash,asOf,runId,receiptHash,dataAsOf,executionMode,modelMode,status,universeSize,summary,decisions,model,executionDecisions}`. It is exposed through `dashboard.research.latest` and `.history`. Idempotency keys and secrets are excluded. This chain never manufactures actual daily NAV, holdings or fills; those facts continue to use the account evidence API. The UI shows actual account observation time independently and marks expired instructions.

The generic Bearer publisher validates the declared contract and verifies the website write receipt. It does not independently obtain the private model call artifacts. A token holder must first run the controlled `receipt` and independent `verify`; a valid-looking SHA alone is not genuine model proof. The supported sync below checks both results, the original request/receipt hashes, and exact equality between the envelope summary and the hash-bound original summary. It preserves the model summary and execution cards without rewriting them.

## Controlled decision sync from Codex or another AI

`pnl/sync-research.mjs` runs through the complete checkout's **controlled** `scripts/tiger-agent.mjs`. It calls doctor and describe, submits only the fixed four-field request, observes its original run ID and obtains receipt plus independent verify before posting. The default and only publication profile is `us-public-model-required-paper-v1`. Legacy profiles remain available for historical/research use through the controlled runner, but cannot create new website instructions.

Configure these values in protected external runner configuration, outside Git:

| Variable | Required value |
|---|---|
| `TIGER_AGENT_PYTHON` | Absolute path to the supported Python environment, validated by doctor. |
| `TIGER_AGENT_STATE_ROOT` | Absolute private model-required runner state directory; do not reuse an old profile's account identity. |
| `TIGER_AGENT_PERMISSION` | Exactly `paper`, selected in trusted startup configuration. |
| `TIGER_PNL_SYNC_STATE_DIR` | Absolute private directory outside source for persistent request, run, verified payload and website receipt. |
| `TIGER_PNL_RESEARCH_PROFILE` | `us-public-model-required-paper-v1`; omitted means this same required profile. |
| `LLM_BACKEND`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | Fixed DeepSeek API configuration from [AGENT-CLIENTS.md](../docs/AGENT-CLIENTS.md). The actual key comes only from protected environment, never JSON requests or repository files. |
| `TIGER_PNL_WRITE_TOKEN`, `TIGER_PNL_URL` | Website Bearer secret and HTTPS origin. Only the narrow four LLM variables survive to required model workers; the website token is never passed to Python. |

Run `node pnl/sync-research.mjs --date YYYY-MM-DD --profile us-public-model-required-paper-v1`. Omitted date means current `America/New_York` date; it labels the stable scheduled identity and does not request historical bars. CLI flags cannot change Python, roots, permission, stages, provider, model or thresholds. Model unavailability, malformed output, missing mandatory-stage proof, native search failure or rule replacement means failure; no older output is silently republished as current.

The request key is `tigerpnl-research-<profile>-<date>`; publication key is `pnl-<profile>-<date>`. State is atomic, fsynced, mode `0600` and persisted before each mutating submission. A missing acceptance response is recovered with exactly the original request/key. Once known, the run ID is never replaced. Each invocation observes for at most 50 seconds; `pending` means the accepted worker continues. Repeat the identical date/profile command to resume. A terminal failure remains attached to that identity; do not generate a new key to conceal it.

To import an already completed required-model run, add `--run-id run-<32-hex-digits>` to that same command. Import never submits another run. Both independent operations and original evidence hashes are still mandatory. The selected profile/request/receipt must agree. A lost website response retries the identical persisted payload without running Tiger again. A published identity returns `already_published` only after checking its persistent receipt metadata.

`asOf` uses the original sealed completion time; `dataAsOf` uses the actual last benchmark bar. `summary`, `model` and `executionDecisions` are copied exactly from verified `action_summary`, `model` and `execution_decisions`; the simple `decisions` mirror model text/actions/weights. Neither `pick.card` nor quantitative watch evidence is concatenated into the explanation. All protected credential values are rejected before sync-state persistence.

The local PID lock rejects active observers and conservatively reclaims a provably dead owner with an inode recheck. Like the ledger's startup guard, this is not an atomic cross-process stale-lock mutex. Production Linux schedulers should wrap the observer with an OS-level lock (`flock`) and keep one observer per date/profile. On macOS, use one serialized Codex chat observer, retain the same date/profile identity and never run simultaneous manual observers. Failed observations retain the original run ID and request; damaged evidence or rejected publication fails closed. No periodic schedule is implied merely by installing the script.

The full historical comparison and its limitations are in [TIGER-ACTIVITY-EVALUATION.md](../docs/TIGER-ACTIVITY-EVALUATION.md). That historical comparison used legacy model-disabled profiles; it is not evidence of the new model-required assessment or its future returns.

## Private backup and isolated restore verification

Run `node pnl/backup.mjs --data-dir /var/lib/tiger-pnl --destination /var/backups/tiger-pnl/<unique-name>`. Both paths must be absolute. The destination must not exist and must be outside the live source and repository, including through symlinks. Failed backups preserve the incomplete destination for protected inspection; retries use a new destination, never overwrite an earlier backup.

The source remains read-only while the website keeps appending. The tool captures each chain's current contiguous immutable filename prefix, copies only those accepted files, fsyncs the new copies, and excludes locks and uncommitted `.pending-*` files. The two chains are independent, so their prefixes can reflect slightly different instants. It then opens the copied account chain with `Ledger` and the copied research chain with `ResearchLedger`, verifies their actual schemas, order, valuation rules, hashes and chain links, closes both, and writes `backup-summary.json` only after successful isolated verification. No live restore or production file replacement is performed.

Successful stdout is a compact summary containing time, each chain's file count and head hash. It contains no private account contents, evidence references, attestors or credentials. Backup records remain private (`0700` directories, `0600` record files). The service operator must preserve those permissions and keep backups outside the public static tree. A systemd backup timer can generate a fresh UTC timestamped destination; a successful verified backup is a recoverable artifact, not permission to overwrite production.

## Verified implementation references

The implementation uses the official Node 24 [filesystem API](https://nodejs.org/docs/latest-v24.x/api/fs.html) for exclusive files, hard links and fsync, [crypto API](https://nodejs.org/docs/latest-v24.x/api/crypto.html#cryptotimingsafeequala-b) for hashed constant-time credential comparisons, and [HTTP API](https://nodejs.org/docs/latest-v24.x/api/http.html#serverrequesttimeout) for explicit request limits and timeouts. These references were checked during implementation; deployment must verify the actual runtime path and version.

Deployment, rollback, exact-domain TLS activation and operator checks are documented in [deploy/README.md](deploy/README.md).


## Public trading whitepaper

The unchanged historical PDF is served at `/whitepaper/tiger-trading-20260730.pdf` with `application/pdf`, exact Content-Length and existing static path/security boundaries. It is the original 2026-07 document (29 pages), SHA-256 `85f5abbd26b1f76c765255ce83ba73af88282e58b480887f2464e8f6e872dc67`, 4,104,128 bytes. Include it in the pinned `pnl/` release and verify source, archive and HTTPS download bytes. GET and HEAD work without the API being available. Its older model-passthrough and simulation descriptions do not weaken the current required-model execution contract. The page labels the document's original date and provides native read/download links.

## Reader interface

The frontend follows the original Tiger report’s black-and-gold identity and pale-green research cards. Three accessible main tabs separate research, actual accounts, and methods. Research has overview, stock details and history tabs; stock-specific tabs expose the model’s full reasons, conditions, frozen market evidence and original sources. Arrow keys, Home and End switch tabs, and direct section links reveal their parent panel.

The first screen shows research generation time and real-account observation time separately. Expired instructions remain historical, and an account with no new snapshot retains its original values and date. A failed refresh removes current instruction cards while keeping previously read account values visibly timestamped. Refresh reads the same public API; it never submits research or trades. Method explanations and the historical whitepaper remain separate from account performance.

All account history points come from the complete dashboard history. The execution list deduplicates actual fills from the latest 20 public account snapshots returned by the dashboard; the interface states that scope. It never synthesizes missing fills or historical performance. The public contract has no current MRS/SHS/ICS or three-market fields, so the interface does not transplant old static report scores.
