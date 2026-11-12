import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile, readdir, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Ledger, ResearchLedger } from '../lib/ledger.mjs';
import { digest, validateRecord } from '../lib/schema.mjs';
import { dashboard } from '../lib/projection.mjs';
import { modelResearchFixture } from './model-fixture.mjs';

// Deliberately synthetic unit-test fixture; never copied into runtime storage.
export function fixture(overrides = {}) {
  return { schemaVersion: 1, idempotencyKey: 'test-only-record-0001', asOf: '2026-01-02T21:00:00Z', account: { label: 'TEST ONLY PRIVATE ACCOUNT', currency: 'USD', nav: 100000, cash: 100000 }, positions: [], trades: [], decisions: [], netExternalFlow: 0, provenance: { kind: 'operator_attested', evidenceSha256: digest('UNIT TEST ONLY - NOT REAL ACCOUNT DATA'), sourceRef: 'unit-test-fixture-private-reference', attestedBy: 'unit-test-private-attestor' }, ...overrides };
}
async function storage(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-test-'));
  return path;
}

test('schema rejects unknown sensitive fields, unsupported sources, impossible times and non-finite numbers', () => {
  assert.doesNotThrow(() => validateRecord(fixture()));
  const cases = [
    { ...fixture(), brokerAccountNumber: 'not-allowed' },
    fixture({ account: { ...fixture().account, token: 'not-allowed' } }),
    fixture({ account: { ...fixture().account, nav: NaN } }),
    fixture({ account: { ...fixture().account, cash: Infinity } }),
    fixture({ asOf: '2026-02-31T21:00:00Z' }),
    fixture({ asOf: '2026-01-02' }),
    fixture({ idempotencyKey: 'short' }),
    fixture({ provenance: { ...fixture().provenance, kind: 'paper' } }),
    fixture({ provenance: { ...fixture().provenance, sourceRef: 'reports/paper-account.json' } }),
    fixture({ provenance: { ...fixture().provenance, evidenceSha256: '0'.repeat(64) } }),
    fixture({ decisions: [{ id: 'r1', symbol: 'AAPL', action: 'buy', targetWeight: 0.2, thesis: '<img onerror=alert(1)>', decidedAt: '2026-01-02T20:00:00Z', runId: 'research-run' }] }),
  ];
  for (const candidate of cases) assert.throws(() => validateRecord(candidate), { status: 422 });
});

test('position valuation and target weight constraints enforce reconcilable snapshots', () => {
  const p = { symbol: 'AAPL', name: 'Apple', quantity: 10, avgCost: 90, price: 100, marketValue: 1000 };
  const base = fixture({ account: { ...fixture().account, cash: 99000 }, positions: [p] });
  assert.doesNotThrow(() => validateRecord(base));
  assert.throws(() => validateRecord({ ...base, positions: [{ ...p, marketValue: 1001 }] }), /marketValue/);
  assert.throws(() => validateRecord({ ...base, positions: [p, p] }), /duplicate/);
  assert.throws(() => validateRecord({ ...base, account: { ...base.account, nav: 99999 } }), /nav must equal/);
});

test('empty dashboard has no fabricated baseline, history or metrics', () => {
  const output = dashboard([], 'test-time');
  assert.deepEqual(output, { schemaVersion: 1, status: 'awaiting_first_record', generatedAt: 'test-time', baseline: null, latest: null, metrics: null, history: [], records: [], research: { latest: null, history: [] } });
});

test('identical concurrent retries commit once; changed key payload conflicts; lock protects process ownership', async t => {
  const path = await storage(t); const store = await Ledger.open(path); t.after(async () => { await store.close(); await rm(path, { recursive: true, force: true }); });
  const input = fixture();
  const receipts = await Promise.all(Array.from({ length: 12 }, () => store.append(input)));
  assert.equal(store.entries.length, 1); assert.equal(receipts.filter(r => !r.replayed).length, 1);
  assert.equal(new Set(receipts.map(r => r.hash)).size, 1);
  await assert.rejects(store.append({ ...input, account: { ...input.account, cash: 100001, nav: 100001 } }), { code: 'IDEMPOTENCY_CONFLICT' });
  await assert.rejects(Ledger.open(path), /already owned/);
  const files = await readdir(join(path, 'records'));
  assert.deepEqual(files, ['000000000001.json']);
  const info = await stat(join(path, 'records', files[0])); assert.equal(info.mode & 0o777, 0o600);
});

test('different simultaneous valid snapshots serialize and recover after restart', async t => {
  const path = await storage(t); const store = await Ledger.open(path);
  const input = fixture();
  await Promise.all(Array.from({ length: 8 }, (_, i) => store.append({ ...input, idempotencyKey: `test-concurrent-${i}`, asOf: `2026-01-${String(i + 2).padStart(2, '0')}T21:00:00Z` })));
  const lastHash = store.entries.at(-1).hash; await store.close();
  const reopened = await Ledger.open(path); t.after(async () => { await reopened.close(); await rm(path, { recursive: true, force: true }); });
  assert.equal(reopened.entries.length, 8); assert.equal(reopened.entries.at(-1).hash, lastHash);
  assert.equal(reopened.getReceipt('test-concurrent-7').sequence, 8);
  assert.equal((await reopened.append({ ...input, idempotencyKey: 'test-concurrent-7', asOf: '2026-01-09T21:00:00Z' })).replayed, true);
});

test('restart refuses tampered data, skipped sequences and broken hashes', async t => {
  const path = await storage(t); const store = await Ledger.open(path);
  t.after(() => rm(path, { recursive: true, force: true }));
  await store.append(fixture()); await store.close();
  const file = join(path, 'records', '000000000001.json');
  const original = await readFile(file, 'utf8'); const changed = JSON.parse(original);
  changed.record.account.cash = 200000; changed.record.account.nav = 200000;
  await writeFile(file, JSON.stringify(changed));
  await assert.rejects(Ledger.open(path), /integrity/);
  await writeFile(file, original);
  await writeFile(join(path, 'records', '000000000003.json'), original);
  await assert.rejects(Ledger.open(path), /integrity/);
});

test('an existing final file is never overwritten and a durable write failure blocks later writes', async t => {
  const path = await storage(t); const store = await Ledger.open(path);
  t.after(async () => { await store.close(); await rm(path, { recursive: true, force: true }); });
  const final = join(path, 'records', '000000000001.json'); await writeFile(final, 'existing-immutable-file');
  await assert.rejects(store.append(fixture()), { code: 'LEDGER_WRITE_FAILED' });
  assert.equal(await readFile(final, 'utf8'), 'existing-immutable-file'); assert.equal(store.entries.length, 0); assert.equal(store.failed, true);
  await assert.rejects(store.append(fixture()), { code: 'LEDGER_UNAVAILABLE' });
});

test('uncommitted temporary files are discarded on restart without creating history', async t => {
  const path = await storage(t); const store = await Ledger.open(path); await store.close();
  await writeFile(join(path, 'records', '.pending-abcdef-123'), 'interrupted write');
  const restarted = await Ledger.open(path);
  t.after(async () => { await restarted.close(); await rm(path, { recursive: true, force: true }); });
  assert.equal(restarted.entries.length, 0); assert.deepEqual(await readdir(join(path, 'records')), []);
});

test('period ordering, account binding, first-flow and duplicate executions are rejected', async t => {
  const path = await storage(t); const store = await Ledger.open(path); t.after(async () => { await store.close(); await rm(path, { recursive: true, force: true }); });
  await assert.rejects(store.append(fixture({ netExternalFlow: 100000 })), { code: 'BASELINE_FLOW_MUST_BE_ZERO' });
  const trade = { id: 'test-only-fill-1', symbol: 'AAPL', side: 'buy', executedAt: '2026-01-02T20:00:00Z', quantity: 1, price: 100, fees: 1 };
  await store.append(fixture({ trades: [trade] }));
  await assert.rejects(store.append(fixture({ idempotencyKey: 'test-order-earlier' })), { code: 'AS_OF_NOT_INCREASING' });
  const next = fixture({ idempotencyKey: 'test-only-record-0002', asOf: '2026-01-03T21:00:00Z' });
  await assert.rejects(store.append({ ...next, account: { ...next.account, label: 'another private alias' } }), { code: 'ACCOUNT_MISMATCH' });
  await assert.rejects(store.append({ ...next, netExternalFlow: 100001 }), { code: 'INVALID_EXTERNAL_FLOW' });
  await assert.rejects(store.append({ ...next, trades: [trade] }), { code: 'DUPLICATE_TRADE' });
  await assert.rejects(store.append({ ...next, trades: [{ ...trade, id: 'old-new-id' }] }), { code: 'TRADE_OUTSIDE_PERIOD' });
});

test('deposit and withdrawal adjusted returns, observed drawdown and public redaction are correct', async t => {
  const path = await storage(t); const store = await Ledger.open(path); t.after(async () => { await store.close(); await rm(path, { recursive: true, force: true }); });
  await store.append(fixture());
  const push = (key, date, nav, flow) => store.append(fixture({ idempotencyKey: key, asOf: date, account: { ...fixture().account, nav, cash: nav }, netExternalFlow: flow }));
  await push('test-return-day-2', '2026-01-03T21:00:00Z', 151000, 50000); // 1% return, 50k deposit
  await push('test-return-day-3', '2026-01-04T21:00:00Z', 139490, -10000); // -1%, 10k withdrawal
  const output = dashboard(store.entries);
  assert.equal(output.history[0].returnPct, 0); assert.equal(output.history[1].returnPct, 1);
  assert.equal(output.metrics.returnPct, -0.01); assert.equal(output.metrics.drawdownPct, -1);
  assert.equal(output.metrics.netExternalFlow, 40000); assert.equal(output.baseline.nav, 100000);
  assert.equal(output.records[0].sequence, 3);
  const serialized = JSON.stringify(output);
  for (const secret of ['provenance', 'attestedBy', 'evidenceSha256', 'sourceRef', 'TEST ONLY PRIVATE ACCOUNT', 'unit-test-private-attestor', 'unit-test-fixture-private-reference', 'idempotencyKey']) assert.equal(serialized.includes(secret), false, secret);
});

test('research hash chain independently restarts and refuses tampering', async t => {
  const path = await storage(t); const store = await ResearchLedger.open(path);
  t.after(() => rm(path, { recursive: true, force: true }));
  const record = modelResearchFixture({ idempotencyKey: 'ledger-research-only-test' });
  const accepted = await store.append(record); await store.close();
  const reopened = await ResearchLedger.open(path); assert.equal(reopened.entries.length, 1); assert.equal(reopened.getReceipt(record.idempotencyKey).hash, accepted.hash); await reopened.close();
  const file = join(path, 'records', '000000000001.json'); const changed = JSON.parse(await readFile(file, 'utf8')); changed.record.summary = 'Tampered test summary'; await writeFile(file, JSON.stringify(changed));
  await assert.rejects(ResearchLedger.open(path), /integrity/);
});
