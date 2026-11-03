import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildResearch, invokeAgent, parseArgs, syncResearch, SyncError } from '../sync-research.mjs';
import { publishRecord } from '../publish.mjs';
import { canonical } from '../lib/schema.mjs';
import { modelFixture, instructionFixture } from './model-fixture.mjs';

const DATE = '2026-01-03'; const PROFILE = 'us-public-model-required-paper-v1'; const RUN = `run-${'a'.repeat(32)}`;
const TEST_TOKEN = 'SYNC-UNIT-TEST-ONLY-NEVER-A-REAL-SECRET';
const hash = value => createHash('sha256').update(value).digest('hex');
function envelope(operation, changes = {}) { return { schema_version: 'tiger.agent-result/v1', operation, operation_ok: true, run_id: RUN, status: 'completed', execution_mode: 'paper', data: {}, summary: null, verification: null, limitations: [], evidence_refs: [], error: null, ...changes }; }
async function setup(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-sync-test-')); t.after(() => rm(path, { recursive: true, force: true }));
  const stateRoot = join(path, 'agent'); const runDir = join(stateRoot, 'runs', RUN); await mkdir(runDir, { recursive: true });
  const originalRequest = { schema_version: 'tiger.agent-request/v1', profile: PROFILE, paper_account: 'public-usd', idempotency_key: `tigerpnl-research-${PROFILE}-${DATE}` };
  const raw = { schema: 'tiger.receipt.v1', run_id: RUN, profile: PROFILE, paper_account: 'public-usd', status: 'completed', sealed_at_utc: '2026-01-03T20:01:30.123456+00:00' };
  const refs = [];
  const summary = { picks: [{ ticker: 'AAPL', position_pct: 0.1, card: 'TEST RESEARCH ONLY\nPrice < 120; watch risk.' }], watchlist: [{ ticker: 'MSFT', evidence: ['TEST observation only'], stop_plan: 'TEST research risk' }], positions: [{ ticker: 'FAKE', shares: 999999 }], pending: [{ ticker: 'FAKE' }], cash_usd: 987654321, equity_usd: 987654321, market_value_usd: 987654321, action: 'TRADE', market_view: 'UNIT TEST RESEARCH FIXTURE ONLY', as_of: { market_date: DATE, market_timezone: 'America/New_York', admitted_at_utc: '2026-01-03T20:00:00Z', benchmark_last_bar: '2026-01-02', freshness: {} }, disclosures: { synthetic: false, execution_mode: 'paper', model_mode: 'enabled', model_required: true, data_coverage: '9/10', scan_stats: { universe_size: 10 } } };
  summary.model = modelFixture();
  summary.action_summary = 'UNIT TEST ONLY: reviewed model instructions, never a real business result.';
  summary.execution_decisions = [instructionFixture({ action: 'buy' }), instructionFixture({ symbol: 'MSFT' })];
  raw.summary = structuredClone(summary);
  for (const [name, object] of [['receipt.json', raw], ['request.json', originalRequest]]) { const bytes = Buffer.from(JSON.stringify(object)); await writeFile(join(runDir, name), bytes); refs.push({ path: name, sha256: hash(bytes), bytes: bytes.length, schema: name === 'receipt.json' ? 'tiger.receipt.v1' : 'tiger.agent-request/v1' }); }
  const verified = { ok: true, receipt_sha256: refs[0].sha256, evidence_count: 42, checks: ['fixture-only-check'] };
  const receipt = envelope('receipt', { summary, verification: verified, evidence_refs: refs });
  const verification = envelope('verify', { verification: verified, evidence_refs: refs, data: { source: { tree_sha256: 'b'.repeat(64) } } });
  const env = { TIGER_AGENT_PYTHON: '/test/trusted/python', TIGER_AGENT_STATE_ROOT: stateRoot, TIGER_AGENT_PERMISSION: 'paper', TIGER_PNL_SYNC_STATE_DIR: join(path, 'sync'), TIGER_PNL_WRITE_TOKEN: TEST_TOKEN, TIGER_PNL_URL: 'https://example.com', LLM_BACKEND: 'api', LLM_BASE_URL: 'https://api.deepseek.com', LLM_MODEL: 'deepseek-flash', LLM_API_KEY: 'SYNC-UNIT-TEST-ONLY-MODEL-KEY' };
  const calls = []; const published = [];
  const invoke = async (operation, input) => { calls.push({ operation, input }); if (operation === 'doctor') return envelope(operation, { status: 'ready', data: { access_mode: 'paper' } }); if (operation === 'describe') return envelope(operation, { status: 'ready', data: { profile: { profile: input.profile, execution: { model_mode: 'enabled' }, model_contract: { provider: 'deepseek', model: 'deepseek-flash', release: 'DeepSeek-V4.1-Flash-1017', required: true, search_mode: 'provider_native_search' } }, access_mode: 'paper' } }); if (operation === 'run') return envelope(operation, { status: 'accepted' }); if (operation === 'status') return envelope(operation); if (operation === 'receipt') return receipt; if (operation === 'verify') return verification; throw new Error('Unexpected operation'); };
  const publish = async options => {
    published.push(options);
    const record = options.record;
    const entry = { sequence: 1, acceptedAt: '2026-01-03T20:02:00.000Z',
      previousHash: '0'.repeat(64), payloadHash: hash(canonical(record)), record };
    const { record: _, ...receipt } = entry;
    return { ...receipt, schemaVersion: 1, idempotencyKey: record.idempotencyKey, asOf: record.asOf,
      hash: hash(canonical(entry)), replayed: false };
  };
  const stateFile = join(env.TIGER_PNL_SYNC_STATE_DIR, `${PROFILE}-${DATE}.json`);
  return { path, env, originalRequest, raw, runDir, receipt, verification, calls, published, invoke, publish, stateFile };
}

test('verified sync calls the controlled six operations, publishes research only, and persists no token or paper holdings', async t => {
  const f = await setup(t);
  const result = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish });
  assert.equal(result.status, 'published_and_verified'); assert.deepEqual(f.calls.map(c => c.operation), ['doctor', 'describe', 'run', 'status', 'receipt', 'verify']);
  const payload = f.published[0].record; assert.equal(payload.decisions[0].action, 'buy'); assert.equal(payload.decisions[0].targetWeight, 0.1); assert.equal(payload.decisions[1].action, 'watch');
  assert.equal(payload.dataAsOf, '2026-01-02'); assert.equal(payload.asOf, '2026-01-03T20:01:30.123Z'); assert.equal(payload.modelMode, 'enabled');
  assert.equal(Object.hasOwn(payload, 'positions'), false); assert.equal(Object.hasOwn(payload, 'account'), false); assert.equal(Object.hasOwn(payload, 'trades'), false);
  const saved = await readFile(f.stateFile, 'utf8'); for (const forbidden of [TEST_TOKEN, '987654321', 'FAKE', 'cash_usd', 'positions', 'pending']) assert.equal(saved.includes(forbidden), false, forbidden);
  const again = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }); assert.equal(again.status, 'already_published'); assert.equal(f.published.length, 1);
  assert.equal((await readdir(f.env.TIGER_PNL_SYNC_STATE_DIR)).some(name => name.endsWith('.lock')), false);
});

test('lost acceptance response preserves exactly the same original request and daily key on recovery', async t => {
  const f = await setup(t); let dropped = false;
  const invoke = async (operation, input, ...rest) => { if (operation === 'run' && !dropped) { dropped = true; f.calls.push({ operation, input }); throw new SyncError('OBSERVATION_TIMEOUT', 'lost response'); } return f.invoke(operation, input, ...rest); };
  const first = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }); assert.equal(first.status, 'pending'); assert.equal(first.runId, null); assert.equal(first.workerCancellationRequested, false);
  const second = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }); assert.equal(second.status, 'published_and_verified');
  const requests = f.calls.filter(c => c.operation === 'run'); assert.equal(requests.length, 2); assert.deepEqual(requests[0].input, requests[1].input);
});

test('accepted running research returns pending, resumes the existing ID, and never replaces it', async t => {
  const f = await setup(t); let running = true;
  const invoke = async (operation, input, ...rest) => operation === 'status' && running ? envelope('status', { status: 'running' }) : f.invoke(operation, input, ...rest);
  const first = await syncResearch({ env: f.env, date: DATE, profile: PROFILE, observeMs: 1100 }, { invoke, publish: f.publish }); assert.equal(first.status, 'pending'); assert.equal(first.runId, RUN); assert.equal(f.published.length, 0);
  running = false; const second = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }); assert.equal(second.status, 'published_and_verified'); assert.equal(f.calls.filter(c => c.operation === 'run').length, 1);
});

test('lost website response retries the persisted identical payload without rerunning research', async t => {
  const f = await setup(t); let dropped = false;
  const publish = async options => { if (!dropped) { dropped = true; f.published.push(options); throw new TypeError('network response lost'); } return f.publish(options); };
  assert.equal((await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish })).status, 'pending');
  assert.equal((await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish })).status, 'published_and_verified');
  assert.deepEqual(f.published[0].record, f.published[1].record); assert.equal(f.calls.filter(c => c.operation === 'run').length, 1); assert.equal(f.calls.filter(c => c.operation === 'verify').length, 1);
});

test('an HTML proxy rejection is reported instead of indefinitely retrying the unchanged payload', async t => {
  const f = await setup(t);
  const publish = options => publishRecord({
    ...options, fetcher: async () => new Response('<html>Payload rejected</html>', { status: 413 }),
  });
  await assert.rejects(syncResearch({ env: f.env, date: DATE }, { invoke: f.invoke, publish }), {
    code: 'PUBLICATION_REJECTED', message: /413/,
  });
  const state = JSON.parse(await readFile(f.stateFile, 'utf8'));
  assert.equal(state.runId, RUN);
  assert.equal(state.phase, 'verified');
  assert.equal(state.websiteReceipt, null);
});

test('missing receipt/failed run/mismatched verification cannot publish and retain their run identity', async t => {
  const f = await setup(t);
  const invoke = async (operation, input, ...rest) => operation === 'receipt' ? envelope('receipt', { operation_ok: false, status: 'invalid', summary: null, verification: null, error: { code: 'EVIDENCE_MISSING', message: 'fixture missing', run_id: RUN, stage: null, retryable: false, details_ref: null } }) : f.invoke(operation, input, ...rest);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish })); assert.equal(f.published.length, 0); assert.equal(JSON.parse(await readFile(f.stateFile, 'utf8')).runId, RUN);
  f.verification.verification = { ...f.verification.verification, receipt_sha256: 'f'.repeat(64) };
  await assert.rejects(buildResearch({ receipt: f.receipt, verification: f.verification, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, profile: PROFILE, idempotencyKey: 'test-bad-verification' }), /both pass/);
  const failed = async (operation, input, ...rest) => operation === 'status' ? envelope('status', { status: 'failed' }) : f.invoke(operation, input, ...rest);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: failed, publish: f.publish }), { code: 'RUN_NOT_COMPLETED' });
  assert.equal(JSON.parse(await readFile(f.stateFile, 'utf8')).phase, 'failed'); assert.equal(f.published.length, 0);
});

test('verified artifact tampering and imported profile mismatch are rejected', async t => {
  const f = await setup(t); await writeFile(join(f.runDir, 'receipt.json'), JSON.stringify({ ...f.raw, profile: 'not-allowed' }));
  await assert.rejects(buildResearch({ receipt: f.receipt, verification: f.verification, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, profile: PROFILE, idempotencyKey: 'test-artifact-tampering' }), /size differs|hash differs/);
});

test('explicit existing-run import does not submit run and trusted configuration cannot be overridden by payload', async t => {
  const f = await setup(t);
  const result = await syncResearch({ env: f.env, date: DATE, profile: PROFILE, runId: RUN }, { invoke: f.invoke, publish: f.publish }); assert.equal(result.status, 'published_and_verified'); assert.equal(f.calls.some(c => c.operation === 'run'), false);
  await assert.rejects(syncResearch({ env: { ...f.env, TIGER_AGENT_PERMISSION: 'readonly' }, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'INVALID_TRUSTED_CONFIGURATION' });
  await assert.rejects(syncResearch({ env: f.env, date: '2026-02-31', profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'INVALID_SYNC_REQUEST' });
  assert.throws(() => parseArgs(['--mode', 'live']), /only/); assert.throws(() => parseArgs(['--profile', PROFILE, '--profile', PROFILE]), /only/);
});

test('sync rejects live observers, reclaims a provably dead observer, and omits unexpected receipt fields', async t => {
  const f = await setup(t); await mkdir(f.env.TIGER_PNL_SYNC_STATE_DIR);
  const lock = `${f.stateFile}.lock`; await writeFile(lock, JSON.stringify({ pid: process.pid }));
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'SYNC_BUSY' });
  // PID upper bound is outside ordinary local process allocation on supported hosts.
  let deadPid = 2147483647;
  try { process.kill(deadPid, 0); assert.fail('Test PID unexpectedly exists'); } catch (cause) { assert.equal(cause.code, 'ESRCH'); }
  await writeFile(lock, JSON.stringify({ pid: deadPid }));
  const publish = async options => ({ ...await f.publish(options), arbitraryRemoteField: TEST_TOKEN });
  assert.equal((await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish })).status, 'published_and_verified');
  assert.equal((await readFile(f.stateFile, 'utf8')).includes(TEST_TOKEN), false);
});

test('corrupt persisted publication metadata cannot be reported as already published', async t => {
  const f = await setup(t); await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish });
  const state = JSON.parse(await readFile(f.stateFile, 'utf8')); state.websiteReceipt.payloadHash = 'f'.repeat(64); await writeFile(f.stateFile, JSON.stringify(state));
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'SYNC_STATE_CONFLICT' });
  assert.equal(f.published.length, 1);
});

test('unspecified sync profile requires the verified DeepSeek model profile', async t => {
  const f = await setup(t);
  const result = await syncResearch({ env: f.env, date: DATE }, { invoke: f.invoke, publish: f.publish });
  assert.equal(result.status, 'published_and_verified');
  assert.equal(f.calls.find(call => call.operation === 'describe').input.profile, PROFILE);
  assert.equal(f.calls.find(call => call.operation === 'run').input.profile, PROFILE);
  assert.equal(f.published[0].record.idempotencyKey, `pnl-${PROFILE}-${DATE}`);
});


test('summary must match its original hash-bound receipt and legacy rule profiles cannot be submitted', async t => {
  const f = await setup(t);
  const changed = { ...f.receipt, summary: { ...f.receipt.summary, action_summary: 'unbound replacement' } };
  await assert.rejects(buildResearch({ receipt: changed, verification: f.verification, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, profile: PROFILE, idempotencyKey: 'unbound-summary-test' }), { code: 'EVIDENCE_INVALID' });
  for (const profile of ['us-public-paper-v1', 'us-public-active-paper-v1']) {
    await assert.rejects(syncResearch({ env: f.env, date: DATE, profile }, { invoke: f.invoke, publish: f.publish }), { code: 'INVALID_SYNC_REQUEST' });
  }
  assert.equal(f.calls.length, 0); assert.equal(f.published.length, 0);
});

test('model-generated plain instructions and provider identities are preserved exactly without metric concatenation', async t => {
  const f = await setup(t);
  const built = await buildResearch({ receipt: f.receipt, verification: f.verification, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, profile: PROFILE, idempotencyKey: 'exact-model-contract-test' });
  assert.equal(built.record.schemaVersion, 2);
  assert.deepEqual(built.record.model, f.raw.summary.model);
  assert.deepEqual(built.record.executionDecisions, f.raw.summary.execution_decisions);
  assert.equal(built.record.summary, f.raw.summary.action_summary);
  assert.equal(built.record.decisions[0].thesis, f.raw.summary.execution_decisions[0].action_summary);
  assert.equal(built.record.decisions[0].thesis.includes(f.raw.summary.picks[0].card), false);
});


test('a terminal degraded or failed model identity never publishes or gets replaced', async t => {
  for (const status of ['completed_degraded', 'failed']) {
    const f = await setup(t);
    const invoke = async (operation, input, ...rest) => operation === 'status' ? envelope('status', { status }) : f.invoke(operation, input, ...rest);
    await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }), { code: 'RUN_NOT_COMPLETED' });
    assert.equal(f.published.length, 0);
    const saved = JSON.parse(await readFile(f.stateFile, 'utf8'));
    assert.equal(saved.phase, 'failed'); assert.equal(saved.runId, RUN);
    assert.equal(f.calls.filter(call => call.operation === 'run').length, 1);
  }
});

test('model credentials echoed into an otherwise hash-bound result cannot enter sync state or publication', async t => {
  const f = await setup(t); const secret = 'UNIT-TEST-ONLY-MODEL-SECRET-REJECTION';
  f.env.LLM_API_KEY = secret;
  f.receipt.summary.action_summary = secret; f.raw.summary = structuredClone(f.receipt.summary);
  const bytes = Buffer.from(JSON.stringify(f.raw)); await writeFile(join(f.runDir, 'receipt.json'), bytes);
  f.receipt.evidence_refs[0].sha256 = hash(bytes); f.receipt.evidence_refs[0].bytes = bytes.length;
  f.receipt.verification.receipt_sha256 = hash(bytes);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'STATE_SECRET_REJECTED' });
  assert.equal(f.published.length, 0); assert.equal((await readFile(f.stateFile, 'utf8')).includes(secret), false);
});


test('preflight refuses a model-disabled description and missing protected model configuration before submitting a run', async t => {
  const f = await setup(t);
  const disabled = async (operation, input, ...rest) => operation === 'describe' ? envelope('describe', { status: 'ready', data: { profile: { profile: PROFILE, execution: { model_mode: 'disabled' } }, access_mode: 'paper' } }) : f.invoke(operation, input, ...rest);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: disabled, publish: f.publish }), { code: 'MODEL_REQUIRED' });
  await assert.rejects(syncResearch({ env: { ...f.env, LLM_API_KEY: '' }, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'MODEL_CONFIGURATION_REQUIRED' });
  assert.equal(f.calls.some(call => call.operation === 'run'), false); assert.equal(f.published.length, 0);
  assert.equal(JSON.parse(await readFile(f.stateFile, 'utf8')).runId, null);
});

test('actual nested Node CLI transport forwards only the fixed model environment to a synthetic interpreter', async t => {
  const f = await setup(t); const python = join(f.path, 'synthetic-interpreter.sh');
  // This is an isolated protocol interpreter, never the Tiger business engine.
  // Its response reports booleans and cannot disclose the fixed test credentials.
  const script = `#!/bin/sh
cat >/dev/null
if [ "$LLM_BACKEND" != "api" ] || [ "$LLM_MODEL" != "deepseek-flash" ] || [ "$LLM_BASE_URL" != "https://api.deepseek.com" ] || [ "$LLM_API_KEY" != "SYNC-UNIT-TEST-ONLY-MODEL-KEY" ] || [ -n "$TIGER_PNL_WRITE_TOKEN" ]; then exit 99; fi
printf '%s\n' '${JSON.stringify(envelope('run', { status: 'accepted', data: { synthetic_test_only: true, model_environment_present: true, website_token_absent: true } }))}'
`;
  await writeFile(python, script, { mode: 0o700 });
  const result = await invokeAgent('run', f.originalRequest, { python, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, env: { ...process.env, ...f.env } }, 10000);
  assert.equal(result.status, 'accepted'); assert.equal(result.data.model_environment_present, true);
  assert.equal(result.data.website_token_absent, true); assert.equal(JSON.stringify(result).includes(f.env.LLM_API_KEY), false);
});
