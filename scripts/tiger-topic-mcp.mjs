#!/usr/bin/env node
/** Independent topic tools; paper workflow and capabilities remain separate. */
import { pathToFileURL } from 'node:url';
import { parseJson } from '../bundles/trading/capabilities/workflow-client.mjs';
import { OPERATIONS, WRITE, failure, invoke, parseArgs, permitted, plain, startupOptions } from './tiger-topic.mjs';

const string = { type: 'string', minLength: 1 };
const object = (properties, required = Object.keys(properties)) => ({ type: 'object', properties, required, additionalProperties: false });
export const SCHEMAS = {
  create: object({ question: string, entity_ids: { type: 'array', items: string, maxItems: 50, uniqueItems: true },
    horizon: { enum: ['current', 'week', 'month', 'baseline'] }, material_policy: { type: 'object' }, idempotency_key: string }, ['question', 'idempotency_key']),
  research: object({ topic_id: string, version: { type: 'integer', minimum: 1 }, idempotency_key: string }),
  watch: object({ topic_id: string, version: { type: 'integer', minimum: 1 }, source_ids: { type: 'array', items: string, minItems: 1, maxItems: 150, uniqueItems: true }, cadence_preset: { enum: ['core', 'normal', 'low', 'daily'] }, idempotency_key: string }),
  status: { oneOf: [object({ job_id: string }), object({ watch_id: string })] },
  report: object({ job_id: string }), evidence: object({ snapshot_id: string, document_id: string }, ['snapshot_id']),
  health: object({}), tick: object({}),
};
export const INSTRUCTIONS = 'Tiger topic research is read-only with respect to accounts and orders. Start with health, create a scoped topic only in an authorized acquire/research deployment, then research only in trusted research mode. Preserve idempotency_key and returned job_id. Poll status and independently read report; accepted/running is not completion. Report data is a tiger.topic-report-view/v1 envelope: report is the sealed as-of proof, sealed_receipt_sha256 binds that proof, and current_source_status separately exposes local recorded availability or review warnings. Read both; no_known_warning is not an online recheck, and a later 404 does not prove a historical claim false. A partial report must disclose required gaps. A new document belongs to a new fixed snapshot, never extend an accepted cutoff. Topic tools cannot change sources, permissions, Python, roots, providers or credentials. Watch/tick only collect and detect change; they never start paid semantic analysis. Cancellation stops this observation; a detached accepted research worker may continue.';
export function tools(mode) {
  return OPERATIONS.filter(op => permitted(op, mode)).map(op => ({ name: `tiger_intelligence_${op === 'create' ? 'topic_create' : op}`, title: `Tiger topic ${op}`,
    description: `${op} within the trusted topic namespace. ${op === 'report' ? 'Return report-view with independently verified sealed report and separate current local source warnings; no network recheck or rewrite of historical proof.' : ''}`,
    inputSchema: SCHEMAS[op], annotations: { readOnlyHint: !WRITE.has(op), destructiveHint: false, idempotentHint: true, openWorldHint: WRITE.has(op) } }));
}
export function serve(options, { stdin = process.stdin, stdout = process.stdout, stderr = process.stderr, invokeImpl = invoke } = {}) {
  const config = startupOptions(options), pendingCalls = new Map();
  let state = 'uninitialized', buffer = Buffer.alloc(0), closing = false, outputClosed = false, bufferedOutput = 0, exitCode = 0, drainTimer, resolveClosed;
  const closed = new Promise(done => { resolveClosed = done; });
  const validId = id => Number.isSafeInteger(id) || typeof id === 'string' && id.length > 0 && Buffer.byteLength(id) <= 128;
  function maybeClosed() {
    if (!closing || pendingCalls.size || !outputClosed && bufferedOutput) return;
    clearTimeout(drainTimer); stdin.removeListener('data', data); stdin.removeListener('end', end); stdin.destroy(); resolveClosed(exitCode);
  }
  function shutdown(code = 0) {
    if (!closing) {
      closing = true; state = 'closing'; exitCode = code; stdin.pause(); buffer = Buffer.alloc(0);
      for (const controller of pendingCalls.values()) controller.abort();
      drainTimer = setTimeout(() => { outputClosed = true; stdout.destroy(); maybeClosed(); }, 2500);
    }
    maybeClosed();
  }
  function send(value) {
    if (outputClosed) return;
    const text = JSON.stringify(value) + '\n', size = Buffer.byteLength(text);
    if (bufferedOutput + size > 32 * 1024 * 1024) { shutdown(7); return; }
    bufferedOutput += size;
    try { stdout.write(text, error => { bufferedOutput -= size; if (error) { outputClosed = true; shutdown(130); } maybeClosed(); }); }
    catch { bufferedOutput -= size; outputClosed = true; shutdown(130); }
  }
  const reply = (id, result) => send({ jsonrpc: '2.0', id, result });
  const reject = (id, code, message) => send({ jsonrpc: '2.0', id, error: { code, message } });
  const empty = value => value === undefined || plain(value) && Object.keys(value).every(k => k === '_meta');
  function handle(request) {
    if (!plain(request) || request.jsonrpc !== '2.0' || typeof request.method !== 'string'
      || Object.keys(request).some(k => !['jsonrpc', 'id', 'method', 'params'].includes(k))
      || Object.hasOwn(request, 'id') && !validId(request.id) || request.params !== undefined && !plain(request.params)) {
      reject(null, -32600, 'Invalid JSON-RPC request.'); return;
    }
    const { id, method, params } = request, notification = !Object.hasOwn(request, 'id');
    if (notification) {
      if (method === 'notifications/initialized' && state === 'awaiting_initialized' && empty(params)) state = 'ready';
      else if (method === 'notifications/cancelled' && plain(params)) pendingCalls.get(params.requestId)?.abort();
      return;
    }
    if (pendingCalls.has(id)) { reject(id, -32600, 'Request id already in flight.'); return; }
    if (method === 'ping') { if (empty(params)) reply(id, {}); else reject(id, -32602, 'Invalid ping params.'); return; }
    if (method === 'initialize') {
      if (state !== 'uninitialized' || !plain(params) || typeof params.protocolVersion !== 'string' || !plain(params.capabilities)
        || !plain(params.clientInfo) || typeof params.clientInfo.name !== 'string' || !params.clientInfo.name || typeof params.clientInfo.version !== 'string') {
        reject(id, -32602, 'Invalid initialization.'); return;
      }
      state = 'awaiting_initialized';
      reply(id, { protocolVersion: ['2025-03-26', '2025-06-18', '2025-11-25'].includes(params.protocolVersion) ? params.protocolVersion : '2025-11-25',
        serverInfo: { name: 'tiger-intelligence-topic', version: '1.0.0' }, capabilities: { tools: { listChanged: false } }, instructions: INSTRUCTIONS }); return;
    }
    if (!['tools/list', 'tools/call'].includes(method)) { reject(id, -32601, 'Method not found.'); return; }
    if (state !== 'ready') { reject(id, -32002, 'Initialize first.'); return; }
    if (method === 'tools/list') { if (empty(params)) reply(id, { tools: tools(config.mode) }); else reject(id, -32602, 'Invalid list params.'); return; }
    if (!plain(params) || typeof params.name !== 'string' || Object.keys(params).some(k => !['name', 'arguments', '_meta'].includes(k))) { reject(id, -32602, 'Invalid tool params.'); return; }
    const operation = OPERATIONS.find(op => params.name === `tiger_intelligence_${op === 'create' ? 'topic_create' : op}`);
    if (!operation) { reject(id, -32602, 'Unknown tool.'); return; }
    if (!permitted(operation, config.mode)) {
      const result = failure(operation, 'MODE_DENIED');
      reply(id, { content: [{ type: 'text', text: JSON.stringify(result) }], structuredContent: result, isError: true }); return;
    }
    if (pendingCalls.size >= 4) { reject(id, -32000, 'Too many in-flight requests.'); return; }
    const controller = new AbortController(); pendingCalls.set(id, controller);
    Promise.resolve().then(() => invokeImpl(operation, params.arguments ?? {}, config, { signal: controller.signal }))
      .catch(error => failure(operation, error?.code ?? 'RUNNER_FAILED'))
      .then(result => reply(id, { content: [{ type: 'text', text: JSON.stringify(result) }], structuredContent: result,
        isError: !result.operation_ok || ['failed', 'blocked', 'interrupted'].includes(result.data?.schema === 'tiger.topic-report-view/v1' ? result.data.report?.state : result.data?.state) }))
      .finally(() => { pendingCalls.delete(id); maybeClosed(); });
  }
  function data(chunk) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    let offset = 0;
    while (offset < bytes.length && !closing) {
      const newline = bytes.indexOf(10, offset), endAt = newline < 0 ? bytes.length : newline;
      const part = bytes.subarray(offset, endAt);
      if (buffer.length + part.length > 128 * 1024) { reject(null, -32600, 'Frame too large.'); shutdown(2); return; }
      buffer = Buffer.concat([buffer, part]);
      if (newline < 0) break;
      const frame = buffer; buffer = Buffer.alloc(0); offset = newline + 1;
      if (!frame.toString('utf8').trim()) continue;
      try { handle(parseJson(frame, 128 * 1024)); } catch { reject(null, -32700, 'Invalid JSON frame.'); }
    }
  }
  function end() { if (buffer.length) reject(null, -32700, 'Incomplete frame.'); shutdown(0); }
  stdin.on('data', data); stdin.on('end', end); stdin.on('error', () => shutdown(7)); stdout.on('error', () => { outputClosed = true; shutdown(130); });
  return { closed, shutdown, get state() { return state; } };
}
export async function main(argv = process.argv.slice(2)) {
  let server;
  try { server = serve(parseArgs(argv, true).config); }
  catch { process.stderr.write('Invalid topic MCP startup.\n'); process.exitCode = 2; return; }
  const stop = () => server.shutdown(130); process.once('SIGINT', stop); process.once('SIGTERM', stop);
  process.exitCode = await server.closed; process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
