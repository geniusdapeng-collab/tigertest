import assert from 'node:assert/strict';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { invoke } from './tiger-topic.mjs';
import { INSTRUCTIONS, SCHEMAS, serve } from './tiger-topic-mcp.mjs';

test('create discovery carries the user conclusion count with bounded typed schema', () => {
  assert.deepEqual(SCHEMAS.create.properties.max_conclusions, { type: 'integer', minimum: 1, maximum: 100 });
  assert.ok(!SCHEMAS.create.required.includes('max_conclusions'));
  assert.match(INSTRUCTIONS, /最多三个结论/u);
  assert.match(INSTRUCTIONS, /do not ask the user to write JSON/u);
  assert.equal(SCHEMAS.research.additionalProperties, false);
  assert.equal(SCHEMAS.research.properties.max_conclusions, undefined);
});

test('actual MCP create forwards max3 to Python immutable topic and preserves same key', async context => {
  const python = process.env.TIGER_TEST_PYTHON;
  if (!python) { context.skip('Set TIGER_TEST_PYTHON for actual subprocess contract checks'); return; }
  const root = await mkdtemp(join(tmpdir(), 'topic-max3-'));
  const config = { python, stateRoot: join(root, 'state'), archiveRoot: join(root, 'archive'), mode: 'acquire' };
  const input = new PassThrough(), output = new PassThrough(), replies = new Map(), pending = new Map();
  let buffer = '';
  output.on('data', bytes => {
    buffer += bytes;
    for (;;) {
      const index = buffer.indexOf('\n');
      if (index < 0) break;
      const value = JSON.parse(buffer.slice(0, index)); buffer = buffer.slice(index + 1);
      replies.set(value.id, value); pending.get(value.id)?.(value); pending.delete(value.id);
    }
  });
  const server = serve(config, { stdin: input, stdout: output, stderr: new PassThrough() });
  const send = value => input.write(JSON.stringify(value) + '\n');
  const wait = id => replies.has(id) ? Promise.resolve(replies.get(id)) : new Promise((done, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error('MCP response deadline')); }, 15000);
    pending.set(id, value => { clearTimeout(timer); done(value); });
  });
  try {
    send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'TEST_FIXTURE', version: '1' } } });
    await wait(1);
    send({ jsonrpc: '2.0', method: 'notifications/initialized' });
    send({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
    const listed = await wait(2);
    assert.deepEqual(listed.result.tools.find(tool => tool.name === 'tiger_intelligence_topic_create').inputSchema.properties.max_conclusions, SCHEMAS.create.properties.max_conclusions);
    await assert.rejects(stat(config.stateRoot), { code: 'ENOENT' });
    const request = { question: 'TEST_FIXTURE NVDA calendar，最多三个结论，保留反证与未知', entity_ids: ['us:NVDA'], max_conclusions: 3, idempotency_key: 'same-user-scope' };
    send({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'tiger_intelligence_topic_create', arguments: request } });
    const first = (await wait(3)).result.structuredContent;
    assert.equal(first.operation_ok, true); assert.equal(first.data.max_conclusions, 3);
    assert.equal(first.data.question, request.question);
    const replay = await invoke('create', request, config);
    assert.deepEqual(replay.data, first.data);
    const conflict = await invoke('create', { ...request, max_conclusions: 4 }, config);
    assert.equal(conflict.error.code, 'IDEMPOTENCY_CONFLICT');
    for (const limit of [true, null, 0, 101, 3.5, '3']) {
      const denied = await invoke('create', { ...request, max_conclusions: limit }, config);
      assert.equal(denied.error.code, 'INVALID_REQUEST');
    }
    const readonly = await invoke('health', {}, { ...config, mode: 'readonly' });
    assert.equal(readonly.operation_ok, true);
    await assert.rejects(stat(config.archiveRoot), { code: 'ENOENT' });
  } finally {
    input.end(); await server.closed;
    await rm(root, { recursive: true, force: true });
  }
});
