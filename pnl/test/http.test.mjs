import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request as rawRequest } from 'node:http';
import { createPnlServer } from '../server.mjs';
import { publishRecord } from '../publish.mjs';
import { digest } from '../lib/schema.mjs';
import { modelResearchFixture } from './model-fixture.mjs';

const TEST_TOKEN = 'test-only-token-never-a-real-secret-00000000';
function fixture() {
  return { schemaVersion: 1, idempotencyKey: 'http-test-record-0001', asOf: '2026-01-02T21:00:00Z', account: { label: 'HTTP TEST ONLY PRIVATE', currency: 'USD', nav: 100000, cash: 100000 }, positions: [], trades: [], decisions: [], netExternalFlow: 0, provenance: { kind: 'operator_attested', evidenceSha256: digest('HTTP UNIT TEST FIXTURE NOT REAL'), sourceRef: 'http-unit-test-private-ref', attestedBy: 'http-unit-test-private-attestor' } };
}
function researchFixture() { return modelResearchFixture({ idempotencyKey: 'http-research-test-0001', runId: 'unit-test-research-run-1' }); }

async function setup(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-http-')); const publicDir = join(path, 'public');
  await mkdir(publicDir); await writeFile(join(publicDir, 'index.html'), '<!doctype html><title>Test fixture only</title>');
  await writeFile(join(path, 'private.txt'), 'must not be public'); await symlink(join(path, 'private.txt'), join(publicDir, 'escape.txt'));
  const app = await createPnlServer({ dataDir: join(path, 'private-data'), token: TEST_TOKEN, publicDir });
  await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
  const baseUrl = `http://127.0.0.1:${app.server.address().port}`;
  t.after(async () => { await app.close(); await rm(path, { recursive: true, force: true }); });
  const post = (body, headers = {}) => fetch(`${baseUrl}/api/v1/records`, { method: 'POST', headers: { Authorization: `Bearer ${TEST_TOKEN}`, 'Content-Type': 'application/json', ...headers }, body: typeof body === 'string' ? body : JSON.stringify(body) });
  return { ...app, baseUrl, post };
}

test('HTTP public reads work, all private reads/writes require token and errors never echo secrets', async t => {
  const { baseUrl, post } = await setup(t);
  const health = await fetch(`${baseUrl}/healthz`); assert.equal(health.status, 200);
  const empty = await (await fetch(`${baseUrl}/api/public/dashboard`)).json(); assert.equal(empty.status, 'awaiting_first_record');
  for (const auth of ['', 'Bearer wrong', 'Basic ignored']) {
    const response = await post(fixture(), { Authorization: auth }); assert.equal(response.status, 401);
    assert.equal((await response.text()).includes(TEST_TOKEN), false);
  }
  assert.equal((await fetch(`${baseUrl}/api/v1/receipts/http-test-record-0001`)).status, 401);
  assert.equal((await post(fixture())).status, 201);
  const replay = await post(fixture()); assert.equal(replay.status, 200); assert.equal((await replay.json()).receipt.replayed, true);
  const publicData = await (await fetch(`${baseUrl}/api/public/dashboard`)).json(); assert.equal(publicData.status, 'active'); assert.equal(publicData.latest.account.cash, 100000);
  assert.equal(JSON.stringify(publicData).includes('HTTP TEST ONLY PRIVATE'), false);
  const receipt = await fetch(`${baseUrl}/api/v1/receipts/http-test-record-0001`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } }); assert.equal(receipt.status, 200);
  assert.equal((await post({ ...fixture(), netExternalFlow: 1 })).status, 409);
});

test('HTTP validates content type, key mismatch, invalid JSON, sensitive fields and size', async t => {
  const { baseUrl, post } = await setup(t);
  assert.equal((await post(fixture(), { 'Content-Type': 'text/plain' })).status, 415);
  assert.equal((await post(fixture(), { 'Idempotency-Key': 'different-key' })).status, 400);
  assert.equal((await post('{"invalid":')).status, 400);
  assert.equal((await post({ ...fixture(), accountNumber: 'private-not-supported' })).status, 422);
  assert.equal((await post(JSON.stringify(fixture()).replace('100000', 'NaN'))).status, 400);
  assert.equal((await post(' '.repeat(1_048_577))).status, 413);
  assert.equal((await post(fixture(), { 'Content-Encoding': 'gzip' })).status, 415);
  const chunked = await new Promise((accept, reject) => {
    const req = rawRequest(`${baseUrl}/api/v1/records`, { method: 'POST', headers: { Authorization: `Bearer ${TEST_TOKEN}`, 'Content-Type': 'application/json' } }, response => { let body = ''; response.on('data', chunk => { body += chunk; }); response.on('end', () => accept({ status: response.statusCode, body: JSON.parse(body) })); });
    req.on('error', reject); req.write(' '.repeat(800000)); req.end(' '.repeat(400000));
  });
  assert.equal(chunked.status, 413); assert.equal(chunked.body.error.code, 'BODY_TOO_LARGE');
});

test('public pagination has explicit bounds and static traversal/symlink escapes are refused', async t => {
  const { baseUrl, post } = await setup(t); await post(fixture());
  const page = await (await fetch(`${baseUrl}/api/public/records?limit=1&offset=0`)).json(); assert.equal(page.total, 1); assert.equal(page.records.length, 1);
  for (const query of ['limit=0', 'limit=101', 'offset=-1', 'offset=9007199254740992', 'limit=1&limit=2', 'token=x']) assert.equal((await fetch(`${baseUrl}/api/public/records?${query}`)).status, 400);
  const index = await fetch(baseUrl); assert.equal(index.status, 200); assert.match(index.headers.get('content-security-policy'), /script-src 'self'/);
  const head = await fetch(baseUrl, { method: 'HEAD' }); assert.equal(head.status, 200); assert.equal(await head.text(), '');
  for (const path of ['/escape.txt', '/%2e%2e/private.txt', '/%2eenv', '/%ZZ', '/%00', '/api/not-a-route']) {
    const result = await new Promise((resolve, reject) => { const req = rawRequest(`${baseUrl}${path}`, response => { response.resume(); resolve(response.statusCode); }); req.on('error', reject); req.end(); });
    assert.ok([400, 404].includes(result), `${path}: ${result}`);
  }
  assert.equal((await fetch(baseUrl, { method: 'POST' })).status, 405);
});

test('publisher performs actual post and receipt read-back and forbids remote HTTP/credentials/redirects', async t => {
  const { baseUrl } = await setup(t);
  const receipt = await publishRecord({ baseUrl, token: TEST_TOKEN, record: fixture(), allowLocalHttp: true });
  assert.equal(receipt.sequence, 1); assert.equal(receipt.payloadHash, digest(fixture()));
  for (const url of ['http://example.com', 'http://localhost.evil.example', 'https://user:password@example.com', 'https://example.com/path']) await assert.rejects(publishRecord({ baseUrl: url, token: TEST_TOKEN, record: fixture(), allowLocalHttp: true }));
  await assert.rejects(publishRecord({ baseUrl, token: TEST_TOKEN, record: fixture() }), /HTTPS/);
  let calls = 0;
  const mismatch = async (_url, options) => { calls++; assert.equal(options.redirect, 'error'); return { ok: true, json: async () => ({ receipt: { idempotencyKey: fixture().idempotencyKey, payloadHash: digest(fixture()), hash: calls === 1 ? 'first' : 'different' } }) }; };
  await assert.rejects(publishRecord({ baseUrl: 'https://example.com', token: TEST_TOKEN, record: fixture(), fetcher: mismatch }), /Read-back/);
});

test('publisher rejects incomplete website receipts even when POST and GET agree', async () => {
  const record = fixture();
  const fetcher = async () => Response.json({ receipt: {
    idempotencyKey: record.idempotencyKey, payloadHash: digest(record),
  } });
  await assert.rejects(publishRecord({
    baseUrl: 'https://example.com', token: TEST_TOKEN, record, fetcher,
  }), { code: 'PUBLICATION_RECEIPT_INVALID' });
});

test('startup rejects weak authentication and repository-local data paths', async () => {
  await assert.rejects(createPnlServer({ dataDir: '/tmp/unused', token: 'short' }), /32-512/);
  await assert.rejects(createPnlServer({ dataDir: new URL('../private-data', import.meta.url).pathname, token: TEST_TOKEN }), /outside the source/);
});

test('startup resolves data symlinks before checking source and static directory boundaries', async t => {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-symlink-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  await symlink(new URL('..', import.meta.url).pathname, join(path, 'source-link'));
  await assert.rejects(createPnlServer({ dataDir: join(path, 'source-link'), token: TEST_TOKEN }), /outside the source/);
  const publicDir = join(path, 'public'); await mkdir(publicDir); await symlink(publicDir, join(path, 'public-link'));
  await assert.rejects(createPnlServer({ dataDir: join(path, 'public-link'), publicDir, token: TEST_TOKEN }), /outside the source.*public/);
});

test('independent research publishes without creating or advancing a real account snapshot', async t => {
  const { baseUrl, post } = await setup(t); const research = researchFixture();
  assert.equal((await fetch(`${baseUrl}/api/v1/research`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(research) })).status, 401);
  const accepted = await publishRecord({ baseUrl, token: TEST_TOKEN, record: research, research: true, allowLocalHttp: true });
  assert.equal(accepted.sequence, 1);
  let result = await (await fetch(`${baseUrl}/api/public/dashboard`)).json();
  assert.equal(result.status, 'awaiting_first_record'); assert.equal(result.latest, null); assert.deepEqual(result.history, []);
  assert.equal(result.research.latest.runId, research.runId); assert.equal(result.research.latest.modelMode, 'enabled');
  assert.equal(result.research.latest.executionMode, 'paper'); assert.equal(result.research.latest.receiptHash, research.receiptHash);
  assert.equal(JSON.stringify(result.research).includes('idempotencyKey'), false);
  await post(fixture());
  const next = { ...research, idempotencyKey: 'http-research-test-0002', runId: 'unit-test-research-run-2', asOf: '2026-01-04T21:00:00Z', decisions: [], executionDecisions: [] };
  await publishRecord({ baseUrl, token: TEST_TOKEN, record: next, research: true, allowLocalHttp: true });
  result = await (await fetch(`${baseUrl}/api/public/dashboard`)).json();
  assert.equal(result.latest.asOf, fixture().asOf); assert.equal(result.history.length, 1); assert.equal(result.latest.account.nav, 100000);
  assert.equal(result.research.history.length, 2); assert.equal(result.research.latest.asOf, next.asOf);
  const health = await (await fetch(`${baseUrl}/healthz`)).json(); assert.equal(health.records, 1); assert.equal(health.research, 2);
  assert.equal((await fetch(`${baseUrl}/api/v1/research/receipts/${research.idempotencyKey}`)).status, 401);
});

test('research rejects failed runs, invented holdings and conflicting retries', async t => {
  const { baseUrl } = await setup(t); const original = researchFixture();
  const postResearch = body => fetch(`${baseUrl}/api/v1/research`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${TEST_TOKEN}` }, body: JSON.stringify(body) });
  for (const record of [{ ...original, status: 'failed' }, { ...original, account: { nav: 100000 } }, { ...original, modelMode: 'unknown' }, { ...original, receiptHash: '0'.repeat(64) }, { ...original, universeSize: 1.5 }, { ...original, dataAsOf: '2027-01-01' }, { ...original, decisions: [{ ...original.decisions[0], runId: 'different-unverified-run' }] }]) assert.equal((await postResearch(record)).status, 422);
  assert.equal((await postResearch(original)).status, 201); assert.equal((await postResearch(original)).status, 200);
  assert.equal((await postResearch({ ...original, summary: 'Changed test summary' })).status, 409);
  const duplicate = await postResearch({ ...original, idempotencyKey: 'research-new-key-same-run', asOf: '2026-01-04T21:00:00Z' });
  assert.equal(duplicate.status, 409); assert.equal((await duplicate.json()).error.code, 'DUPLICATE_RESEARCH_RUN');
});
