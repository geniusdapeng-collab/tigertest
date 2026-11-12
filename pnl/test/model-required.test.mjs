import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { canonical, digest, validateResearch } from '../lib/schema.mjs';
import { ResearchLedger } from '../lib/ledger.mjs';
import { createPnlServer } from '../server.mjs';
import { modelResearchFixture, instructionFixture } from './model-fixture.mjs';

const TOKEN = 'MODEL-REQUIRED-UNIT-TEST-ONLY-NOT-A-REAL-SECRET';
function legacyFixture(overrides = {}) {
  return { schemaVersion: 1, idempotencyKey: 'legacy-disabled-test-0001', asOf: '2026-01-02T21:00:00Z', runId: 'old-unit-test-disabled-run', receiptHash: digest('OLD TEST RECEIPT'), dataAsOf: '2026-01-02', executionMode: 'paper', modelMode: 'disabled', status: 'completed_degraded', universeSize: 0, summary: 'Historical test fixture only, model not called.', decisions: [], ...overrides };
}
async function directory(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-model-required-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}
async function writeHistoricalEntry(path, record) {
  await mkdir(join(path, 'records'), { recursive: true });
  const body = { sequence: 1, acceptedAt: '2026-01-02T22:00:00Z', previousHash: '0'.repeat(64), payloadHash: digest(record), record };
  const entry = { ...body, hash: digest(body) };
  await writeFile(join(path, 'records', '000000000001.json'), canonical(entry));
  return entry;
}

test('model-required research accepts the strict model contract and rejects missing stages, downgraded models and forged counters', () => {
  const original = modelResearchFixture();
  assert.doesNotThrow(() => validateResearch(original));
  const cases = [
    { ...original, modelMode: 'disabled' },
    { ...original, modelMode: 'passthrough' },
    { ...original, status: 'completed_degraded' },
    { ...original, model: { ...original.model, model: 'deepseek-v4-pro' } },
    { ...original, model: { ...original.model, release: 'unverified-latest' } },
    { ...original, model: { ...original.model, provider: 'other' } },
    { ...original, model: { ...original.model, required: false } },
    { ...original, model: { ...original.model, all_required_completed: false } },
    { ...original, model: { ...original.model, search_count: 0 } },
    { ...original, model: { ...original.model, call_count: 4 } },
    { ...original, model: { ...original.model, response_ids: [] } },
    { ...original, model: { ...original.model, required_stages: original.model.required_stages.slice(1) } },
    { ...original, model: { ...original.model, stage_counts: { ...original.model.stage_counts, 'decision.debate': 0 } } },
    { ...original, model: { ...original.model, apiKey: 'must-not-enter-contract' } },
  ];
  for (const value of cases) assert.throws(() => validateResearch(value), { status: 422 });
});

test('execution cards bind numeric instructions to the compatibility decision and refuse unsafe source links', () => {
  const buy = modelResearchFixture({ executionDecisions: [instructionFixture({ action: 'buy' })] });
  assert.doesNotThrow(() => validateResearch(buy));
  for (const changes of [
    { target_weight: 0.3 }, { entry_price: null }, { stop_price: 101 }, { shares: 0 },
    { shares: 1.5 }, { risk_usd: -1 }, { why_this: '' }, { valid_for: '' }, { valid_for: '2026-02-31T16:00:00-05:00' },
    { sources: [{ id: 'bad', title: 'bad', url: 'https://example.com/?accessToken=must-not-leak', published_at: null }] },
    { sources: [{ id: 'bad', title: 'bad', url: 'javascript:alert(1)', published_at: null }] },
    { sources: [{ id: 'bad', title: 'bad', url: 'https://user:password@example.com/', published_at: null }] },
  ]) {
    const value = structuredClone(buy); value.executionDecisions[0] = { ...value.executionDecisions[0], ...changes };
    assert.throws(() => validateResearch(value), { status: 422 });
  }
  const watch = modelResearchFixture();
  assert.equal(watch.executionDecisions[0].entry_price, null);
  assert.doesNotThrow(() => validateResearch(watch));
});

function marketSnapshot(changes = {}) {
  const snapshot = { symbol: 'AAPL', as_of: '2026-01-02', close: 100, series_sha256: digest('SYNTHETIC UNIT TEST BAR SERIES ONLY'), ...changes };
  return { id: `md_${snapshot.symbol}_${snapshot.as_of.replaceAll('-', '')}_${snapshot.series_sha256.slice(0, 24)}`, ...snapshot };
}
function marketResearch(instruction = {}, record = {}) {
  return modelResearchFixture({ executionDecisions: [instructionFixture({ market_evidence: [marketSnapshot()], ...instruction })], ...record });
}

test('watch can cite only its frozen own-stock market snapshot while a buy must also cite news', () => {
  const watch = marketResearch({ sources: [] });
  assert.doesNotThrow(() => validateResearch(watch));
  assert.doesNotThrow(() => validateResearch(marketResearch({ action: 'buy' })));
  assert.throws(() => validateResearch(marketResearch({ action: 'buy', sources: [] })), { status: 422 });
  // Stock bars can be newer than the benchmark, provided they are not after assessment.
  assert.doesNotThrow(() => validateResearch(marketResearch({ market_evidence: [marketSnapshot({ as_of: '2026-01-03' })] })));
  const fresh = marketResearch({ market_evidence: [marketSnapshot({ as_of: '2026-01-03' })] }, { asOf: '2026-01-03T02:00:00Z' });
  fresh.decisions[0].decidedAt = fresh.asOf;
  assert.throws(() => validateResearch(fresh), { status: 422 }); // New York is still January 2.
});

test('market evidence rejects wrong identity, missing or extra snapshots, impossible dates and invented fields', () => {
  const badSnapshots = [
    null, [], {}, { ...marketSnapshot(), id: 'md_invented' }, marketSnapshot({ symbol: 'MSFT' }),
    marketSnapshot({ as_of: '2026-01-04' }), marketSnapshot({ as_of: '2026-02-31' }),
    marketSnapshot({ as_of: '0000-01-02' }),
    marketSnapshot({ as_of: '2026-1-2' }), marketSnapshot({ as_of: '2026-01-02T00:00:00Z' }),
    ...[0, -1, NaN, Infinity, '100', null].map(close => marketSnapshot({ close })),
    marketSnapshot({ series_sha256: '0'.repeat(64) }), marketSnapshot({ series_sha256: 'A'.repeat(64) }),
    marketSnapshot({ series_sha256: 'a'.repeat(63) }),
    { ...marketSnapshot(), url: 'https://example.com/not-a-market-provider' },
  ];
  for (const snapshot of badSnapshots) assert.throws(() => validateResearch(marketResearch({ market_evidence: [snapshot] })), { status: 422 });
  for (const evidence of [null, {}, [], [marketSnapshot(), marketSnapshot()]]) assert.throws(() => validateResearch(marketResearch({ market_evidence: evidence })), { status: 422 });
  const missing = marketResearch(); delete missing.executionDecisions[0].market_evidence;
  assert.throws(() => validateResearch(missing), { status: 422 });
});

test('v2 instructions admit buy or watch only and keep market references separate from at most eight news sources', () => {
  for (const action of ['sell', 'hold']) assert.throws(() => validateResearch(marketResearch({ action })), { status: 422 });
  const articles = Array.from({ length: 8 }, (_, index) => ({ id: `test-news-${index}`, title: 'SYNTHETIC NEWS ONLY', url: `https://example.com/test/${index}`, published_at: null }));
  assert.doesNotThrow(() => validateResearch(marketResearch({ action: 'buy', sources: articles })));
  assert.throws(() => validateResearch(marketResearch({ sources: [...articles, { ...articles[0], id: 'ninth-news' }] })), { status: 422 });
  assert.throws(() => validateResearch(marketResearch({ sources: [{ ...articles[0], id: marketSnapshot().id }] })), { status: 422 });
  assert.throws(() => validateResearch(marketResearch({ sources: [articles[0], articles[0]] })), { status: 422 });
});

for (const key of ['entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days']) {
  test(`watch cannot include an executable ${key} while its real closing reference remains visible`, () => {
    const watch = marketResearch({ sources: [] });
    assert.equal(watch.executionDecisions[0].market_evidence[0].close, 100);
    assert.equal(watch.executionDecisions[0][key], null);
    assert.doesNotThrow(() => validateResearch(watch));
    watch.executionDecisions[0][key] = 1;
    assert.throws(() => validateResearch(watch), { status: 422 });
  });
}

test('historical disabled chains still load unchanged while every new disabled append is refused', async t => {
  const path = await directory(t); const old = legacyFixture(); const entry = await writeHistoricalEntry(path, old);
  const store = await ResearchLedger.open(path);
  try {
    assert.equal(store.entries[0].hash, entry.hash);
    assert.deepEqual(store.entries[0].record, old);
    await assert.rejects(store.append(legacyFixture({ idempotencyKey: 'new-disabled-must-fail', asOf: '2026-01-03T21:00:00Z', runId: 'new-disabled-run' })), { code: 'MODEL_REQUIRED' });
    assert.equal(store.entries.length, 1);
    const accepted = await store.append(modelResearchFixture());
    assert.equal(accepted.sequence, 2); assert.equal(accepted.previousHash, entry.hash);
  } finally { await store.close(); }
  const restored = await ResearchLedger.open(path);
  try { assert.equal(restored.entries.length, 2); assert.equal(restored.entries[0].hash, entry.hash); }
  finally { await restored.close(); }
});

test('HTTP publishes model execution instructions without inventing account fills and serves the whitepaper as PDF', async t => {
  const path = await directory(t); const publicDir = join(path, 'public');
  await mkdir(join(publicDir, 'whitepaper'), { recursive: true });
  await writeFile(join(publicDir, 'index.html'), '<!doctype html><title>TEST ONLY</title>');
  const pdf = Buffer.from('%PDF-1.4\nTEST MIME FIXTURE ONLY\n%%EOF');
  await writeFile(join(publicDir, 'whitepaper', 'tiger-trading-20260730.pdf'), pdf);
  const app = await createPnlServer({ dataDir: join(path, 'private'), publicDir, token: TOKEN });
  await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${app.server.address().port}`;
  const post = value => fetch(`${base}/api/v1/research`, { method: 'POST', headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  try {
    const denied = await post(legacyFixture()); assert.equal(denied.status, 422); assert.equal((await denied.json()).error.code, 'MODEL_REQUIRED');
    const invalid = marketResearch({ market_evidence: [marketSnapshot({ symbol: 'MSFT' })], sources: [] });
    assert.equal((await post(invalid)).status, 422);
    assert.equal((await (await fetch(`${base}/healthz`)).json()).research, 0);
    const input = marketResearch({ sources: [] }); const accepted = await post(input); assert.equal(accepted.status, 201);
    const output = await (await fetch(`${base}/api/public/dashboard`)).json();
    assert.equal(output.latest, null); assert.deepEqual(output.records, []);
    assert.deepEqual(output.research.latest.model, input.model);
    assert.deepEqual(output.research.latest.executionDecisions, input.executionDecisions);
    assert.equal(output.research.latest.schemaVersion, 2);
    assert.equal(JSON.stringify(output).includes(TOKEN), false);
    const document = await fetch(`${base}/whitepaper/tiger-trading-20260730.pdf`);
    assert.equal(document.status, 200); assert.match(document.headers.get('content-type'), /^application\/pdf/);
    assert.deepEqual(Buffer.from(await document.arrayBuffer()), pdf);
    const before = await readFile(join(path, 'private', 'research', 'records', '000000000001.json'));
    assert.equal((await post(input)).status, 200);
    assert.deepEqual(await readFile(join(path, 'private', 'research', 'records', '000000000001.json')), before);
  } finally { await app.close(); }
});
