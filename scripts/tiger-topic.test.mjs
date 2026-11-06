import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { invoke, parseArgs, permitted, topicEnvironment } from './tiger-topic.mjs';
import { serve, tools } from './tiger-topic-mcp.mjs';

const startup = { stateRoot: '/tmp/topic-state-fixture', archiveRoot: '/tmp/topic-archive-fixture' };

test('topic startup accepts only trusted roots and capability modes', () => {
  const parsed = parseArgs(['research', '--state-root', startup.stateRoot, '--archive-root', startup.archiveRoot, '--mode', 'research', '--wait']);
  assert.equal(parsed.config.mode, 'research'); assert.equal(parsed.wait, true);
  for (const argv of [
    ['live'], ['health', '--state-root', 'relative', '--archive-root', '/tmp/archive'],
    ['health', '--state-root', '/tmp/state', '--archive-root', '/tmp/archive', '--mode', 'paper'],
    ['health', '--state-root', '/tmp/state', '--archive-root', '/tmp/archive', '--url', 'https://example.com'],
    ['report', '--state-root', '/tmp/state', '--archive-root', '/tmp/archive', '--wait'],
  ]) assert.throws(() => parseArgs(argv));
  assert.equal(permitted('research', 'acquire'), false);
  assert.equal(permitted('watch', 'readonly'), false);
  assert.equal(permitted('report', 'readonly'), true);
});

test('topic environment omits unrelated secrets and readonly model credentials', () => {
  const env = { PATH: process.env.PATH, LLM_BACKEND: 'api', LLM_BASE_URL: 'https://api.deepseek.com', LLM_MODEL: 'deepseek-flash',
    LLM_API_KEY: 'fixture-secret', GITHUB_TOKEN: 'unrelated-token', PYTHONPATH: '/injected/path', TIGER_INTEL_REGISTRY_FILE: '/trusted/registry' };
  const ro = topicEnvironment('readonly', env), rw = topicEnvironment('research', env);
  assert.equal(ro.LLM_API_KEY, undefined); assert.equal(rw.LLM_API_KEY, 'fixture-secret');
  assert.equal(rw.GITHUB_TOKEN, undefined); assert.equal(rw.PYTHONPATH, undefined);
  assert.equal(rw.TIGER_INTEL_REGISTRY_FILE, '/trusted/registry');
});

test('readonly denial and aborted call happen before spawn', async () => {
  let called = 0; const spawnImpl = () => { called++; throw Error('must not spawn'); };
  await assert.rejects(invoke('research', {}, startup, { spawnImpl }), /MODE_DENIED/u);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(invoke('health', {}, startup, { signal: controller.signal, spawnImpl }), /INTERRUPTED/u);
  assert.equal(called, 0);
});

function peer(config, invokeImpl) {
  const input = new PassThrough(), output = new PassThrough(), lines = [];
  let buffer = '';
  output.on('data', bytes => { buffer += bytes; for (;;) { const i = buffer.indexOf('\n'); if (i < 0) break; lines.push(JSON.parse(buffer.slice(0, i))); buffer = buffer.slice(i + 1); } });
  const server = serve({ ...startup, ...config }, { stdin: input, stdout: output, stderr: new PassThrough(), ...(invokeImpl ? { invokeImpl } : {}) });
  const send = value => input.write(JSON.stringify(value) + '\n');
  const initialize = () => { send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'fixture', version: '1' } } }); send({ jsonrpc: '2.0', method: 'notifications/initialized' }); };
  return { input, output, lines, server, send, initialize };
}

test('MCP discovery performs no Python or business work; capabilities differ by mode', async () => {
  let invoked = 0;
  const p = peer({}, () => { invoked++; }); p.initialize();
  p.send({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  p.send({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'tiger_intelligence_research', arguments: {} } });
  assert.equal(invoked, 0);
  const names = p.lines.find(x => x.id === 2).result.tools.map(x => x.name);
  assert.deepEqual(names.sort(), ['tiger_intelligence_status', 'tiger_intelligence_report', 'tiger_intelligence_evidence', 'tiger_intelligence_health'].sort());
  assert.equal(p.lines.find(x => x.id === 3).result.structuredContent.error.code, 'MODE_DENIED');
  assert.ok(tools('research').some(x => x.name === 'tiger_intelligence_topic_create'));
  p.input.end(); assert.equal(await p.server.closed, 0);
});

test('MCP initialization, duplicate IDs, cancellation and error envelopes are explicit', async () => {
  let resolveCall; let aborted = false;
  const p = peer({ mode: 'research' }, (_op, _input, _config, { signal }) => new Promise(done => { resolveCall = done; signal.addEventListener('abort', () => { aborted = true; done({ schema: 'tiger.topic-operation/v1', operation: 'research', operation_ok: false, data: {}, error: { code: 'INTERRUPTED' }, limitations: [] }); }); }));
  p.send({ jsonrpc: '2.0', id: 0, method: 'tools/list' }); assert.equal(p.lines[0].error.code, -32002);
  p.initialize();
  const call = { jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'tiger_intelligence_research', arguments: {} } };
  p.send(call); p.send(call);
  assert.equal(p.lines.find(x => x.id === 4)?.error.code, -32600);
  await new Promise(done => setImmediate(done));
  p.send({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: 4 } });
  await new Promise(done => setImmediate(done));
  assert.equal(aborted, true); assert.equal(typeof resolveCall, 'function');
  assert.ok(p.lines.some(x => x.id === 4 && x.result?.isError));
  p.input.end(); await p.server.closed;
});

test('MCP oversized and incomplete frames terminate with visible protocol errors', async () => {
  for (const oversized of [true, false]) {
    const p = peer({});
    if (oversized) p.input.write(Buffer.alloc(128 * 1024 + 1, 65)); else p.input.end('{');
    assert.equal(await p.server.closed, oversized ? 2 : 0);
    assert.ok(p.lines.some(x => x.error));
  }
});

function reportEnvelope() {
  const receipt = 'a'.repeat(64);
  return { schema: 'tiger.topic-operation/v1', operation: 'report', operation_ok: true, error: null, limitations: [],
    data: { schema: 'tiger.topic-report-view/v1', sealed_receipt_sha256: receipt,
      report: { schema: 'tiger.topic-report/v1', state: 'completed', receipt_sha256: receipt, cutoff: 'TEST_FIXTURE-as-of' },
      current_source_status: { schema: 'tiger.topic-source-status/v1', state: 'needs_review', network_checked: false,
        warnings: [{ source_id: 'TEST_FIXTURE-source', status: 'source_unavailable', http_status: 404 }] } } };
}

test('Node report transport preserves sealed proof and current warnings as separate view fields', async () => {
  const valid = reportEnvelope();
  function spawnResult(result) {
    const child = new EventEmitter();
    child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    setImmediate(() => { child.stdout.end(JSON.stringify(result)); child.emit('close', 0, null); });
    return child;
  }
  const result = await invoke('report', { job_id: 'TEST_FIXTURE' }, startup, { spawnImpl: () => spawnResult(valid) });
  assert.deepEqual(result, valid);
  for (const mutate of [
    view => { view.sealed_receipt_sha256 = 'b'.repeat(64); },
    view => { view.current_source_status.network_checked = true; },
    view => { delete view.current_source_status; },
  ]) {
    const invalid = structuredClone(valid); mutate(invalid.data);
    await assert.rejects(invoke('report', {}, startup, { spawnImpl: () => spawnResult(invalid) }), /RESULT_INVALID/u);
  }
});

test('MCP report view exposes current review warning without changing the sealed completion state', async () => {
  const expected = reportEnvelope(), p = peer({}, async () => expected);
  p.initialize();
  p.send({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name: 'tiger_intelligence_report', arguments: { job_id: 'TEST_FIXTURE' } } });
  await new Promise(done => setImmediate(done));
  const result = p.lines.find(line => line.id === 7).result;
  assert.deepEqual(result.structuredContent, expected); assert.equal(result.isError, false);
  assert.equal(JSON.parse(result.content[0].text).data.current_source_status.state, 'needs_review');
  assert.equal(result.structuredContent.data.report.state, 'completed');
  p.input.end(); assert.equal(await p.server.closed, 0);
});

test('real Python health and topic creation preserve readonly and no-model contracts', async context => {
  const python = process.env.TIGER_TEST_PYTHON;
  if (!python) { context.skip('Set TIGER_TEST_PYTHON for actual subprocess checks'); return; }
  const root = await mkdtemp(join(tmpdir(), 'topic-native-'));
  try {
    const args = ['--python', python, '--state-root', join(root, 'state'), '--archive-root', join(root, 'archive')];
    const health = spawnSync(process.execPath, ['scripts/tiger-topic.mjs', 'health', ...args], { input: '{}', encoding: 'utf8', timeout: 10000 });
    assert.equal(health.status, 0, health.stdout + health.stderr);
    assert.equal(JSON.parse(health.stdout).operation_ok, true);
    await assert.rejects(stat(join(root, 'state')), { code: 'ENOENT' });
    const created = spawnSync(process.execPath, ['scripts/tiger-topic.mjs', 'create', ...args, '--mode', 'acquire'], { input: JSON.stringify({ question: 'NVDA original statements and contrary evidence', entity_ids: ['us:NVDA'], idempotency_key: 'fixture-topic' }), encoding: 'utf8', timeout: 10000 });
    assert.equal(created.status, 0, created.stdout + created.stderr);
    assert.equal(JSON.parse(created.stdout).data.entity_ids[0], 'us:NVDA');
    const bad = spawnSync(process.execPath, ['scripts/tiger-topic.mjs', 'create', ...args], { input: '{}', encoding: 'utf8', timeout: 10000 });
    assert.equal(bad.status, 3); assert.equal(JSON.parse(bad.stdout).error.code, 'MODE_DENIED');
  } finally { await rm(root, { recursive: true, force: true }); }
});
