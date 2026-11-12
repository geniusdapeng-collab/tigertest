import { createServer } from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';
import { mkdir, readFile, realpath, stat } from 'node:fs/promises';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isCliEntry } from './lib/cli.mjs';
import { Ledger, ResearchLedger } from './lib/ledger.mjs';
import { PnlError, validKey } from './lib/schema.mjs';
import { dashboard, publicRecord } from './lib/projection.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const MAX_BODY = 1_048_576;
const MIME = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.txt': 'text/plain; charset=utf-8', '.json': 'application/json; charset=utf-8', '.pdf': 'application/pdf' };
const TOKEN_HASH = token => createHash('sha256').update(token).digest();
function headers(response) {
  response.setHeader('X-Content-Type-Options', 'nosniff'); response.setHeader('Referrer-Policy', 'no-referrer');
  response.setHeader('X-Frame-Options', 'DENY');
  response.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'");
}
function json(response, status, value) { response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }); response.end(JSON.stringify(value)); }
function authorize(request, token) {
  const auth = request.headers.authorization;
  const candidate = typeof auth === 'string' && auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!candidate || candidate.length > 512 || !timingSafeEqual(TOKEN_HASH(candidate), TOKEN_HASH(token))) throw new PnlError(401, 'UNAUTHORIZED', 'A valid Bearer token is required');
}
function parsePage(params) {
  for (const key of params.keys()) if (!['limit', 'offset'].includes(key)) throw new PnlError(400, 'INVALID_PAGINATION', 'Only limit and offset query parameters are supported');
  const limit = params.get('limit') ?? '20'; const offset = params.get('offset') ?? '0';
  if (params.getAll('limit').length > 1 || params.getAll('offset').length > 1 || !/^\d+$/.test(limit) || !/^\d+$/.test(offset) || +limit < 1 || +limit > 100 || !Number.isSafeInteger(+offset)) throw new PnlError(400, 'INVALID_PAGINATION', 'limit must be 1-100 and offset a nonnegative integer');
  return { limit: +limit, offset: +offset };
}
async function readJson(request) {
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(request.headers['content-type'] ?? '')) throw new PnlError(415, 'JSON_REQUIRED', 'Content-Type must be application/json');
  if (request.headers['content-encoding']) throw new PnlError(415, 'ENCODING_NOT_SUPPORTED', 'Compressed request bodies are not supported');
  const length = request.headers['content-length'];
  if (length && (!/^\d+$/.test(length) || +length > MAX_BODY)) throw new PnlError(413, 'BODY_TOO_LARGE', 'Body limit is 1 MiB');
  const chunks = []; let size = 0;
  await new Promise((accept, reject) => {
    const cleanup = () => { request.off('data', onData); request.off('end', onEnd); request.off('error', onError); request.off('aborted', onAborted); };
    const onError = error => { cleanup(); reject(error); };
    const onAborted = () => onError(new PnlError(400, 'REQUEST_ABORTED', 'Request body was interrupted'));
    const onEnd = () => { cleanup(); accept(); };
    const onData = chunk => {
      size += chunk.length;
      if (size > MAX_BODY) { cleanup(); request.resume(); reject(new PnlError(413, 'BODY_TOO_LARGE', 'Body limit is 1 MiB')); return; }
      chunks.push(chunk);
    };
    request.on('data', onData); request.once('end', onEnd); request.once('error', onError); request.once('aborted', onAborted);
  });
  try {
    const body = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
    return JSON.parse(body);
  } catch { throw new PnlError(400, 'INVALID_JSON', 'Body must be valid UTF-8 JSON'); }
}
export async function createPnlServer({ dataDir = process.env.TIGER_PNL_DATA_DIR, token = process.env.TIGER_PNL_WRITE_TOKEN, publicDir = resolve(HERE, 'public') } = {}) {
  if (typeof token !== 'string' || token.length < 32 || token.length > 512 || /\s/.test(token)) throw new Error('TIGER_PNL_WRITE_TOKEN must be a whitespace-free secret of 32-512 characters');
  if (!dataDir) throw new Error('TIGER_PNL_DATA_DIR is required and must be outside the source tree');
  const sourceRoot = await realpath(resolve(HERE, '..')); let dataPath = resolve(dataDir);
  if (dataPath === sourceRoot || dataPath.startsWith(`${sourceRoot}${sep}`)) throw new Error('Data directory must be outside the source tree');
  await mkdir(dataPath, { recursive: true, mode: 0o700 }); dataPath = await realpath(dataPath);
  let publicRoot = resolve(publicDir);
  try { publicRoot = await realpath(publicRoot); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (dataPath === sourceRoot || dataPath.startsWith(`${sourceRoot}${sep}`) || dataPath === publicRoot || dataPath.startsWith(`${publicRoot}${sep}`)) throw new Error('Data directory must be outside the source tree and public static directory');
  const ledger = await Ledger.open(dataPath);
  let researchLedger;
  try { researchLedger = await ResearchLedger.open(resolve(dataPath, 'research')); }
  catch (error) { await ledger.close(); throw error; }
  const server = createServer({ maxHeaderSize: 16_384 }, async (request, response) => {
    headers(response);
    try {
      // Check raw segments before URL parsing, which otherwise normalizes dot segments.
      const rawPath = (request.url ?? '/').split('?')[0]; let decoded;
      try { decoded = decodeURIComponent(rawPath); } catch { throw new PnlError(400, 'INVALID_PATH', 'Path encoding is invalid'); }
      if (!decoded.startsWith('/') || /[\u0000-\u001f\\]/u.test(decoded) || decoded.split('/').some(part => part === '..' || part.startsWith('.'))) throw new PnlError(404, 'NOT_FOUND', 'Resource not found');
      const url = new URL(request.url, 'http://localhost');
      const pathname = url.pathname;
      if (pathname === '/healthz') {
        if (!['GET', 'HEAD'].includes(request.method)) throw new PnlError(405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
        const failed = ledger.failed || researchLedger.failed;
        json(response, failed ? 503 : 200, { status: failed ? 'unavailable' : 'ok', records: ledger.entries.length, research: researchLedger.entries.length, ledger: 'verified' }); return;
      }
      if (pathname === '/api/public/dashboard') {
        if (request.method !== 'GET') throw new PnlError(405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
        json(response, 200, dashboard(ledger.entries, new Date().toISOString(), researchLedger.entries)); return;
      }
      if (pathname === '/api/public/records') {
        if (request.method !== 'GET') throw new PnlError(405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
        const { limit, offset } = parsePage(url.searchParams);
        const entries = ledger.entries.slice().reverse();
        json(response, 200, { schemaVersion: 1, total: entries.length, limit, offset, records: entries.slice(offset, offset + limit).map(publicRecord) }); return;
      }
      if (pathname === '/api/v1/records' || pathname === '/api/v1/research') {
        authorize(request, token);
        if (request.method !== 'POST') throw new PnlError(405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
        const record = await readJson(request);
        if (request.headers['idempotency-key'] && request.headers['idempotency-key'] !== record?.idempotencyKey) throw new PnlError(400, 'IDEMPOTENCY_KEY_MISMATCH', 'Header and body idempotency keys must match');
        const target = pathname === '/api/v1/research' ? researchLedger : ledger;
        const accepted = await target.append(record);
        json(response, accepted.replayed ? 200 : 201, { receipt: accepted }); return;
      }
      if (pathname.startsWith('/api/v1/receipts/') || pathname.startsWith('/api/v1/research/receipts/')) {
        authorize(request, token);
        if (request.method !== 'GET') throw new PnlError(405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
        const research = pathname.startsWith('/api/v1/research/receipts/');
        const key = pathname.slice(research ? '/api/v1/research/receipts/'.length : '/api/v1/receipts/'.length);
        if (!validKey(key)) throw new PnlError(400, 'INVALID_IDEMPOTENCY_KEY', 'Invalid receipt key');
        const found = (research ? researchLedger : ledger).getReceipt(key);
        if (!found) throw new PnlError(404, 'RECEIPT_NOT_FOUND', 'No accepted record has this key');
        json(response, 200, { receipt: found }); return;
      }
      if (pathname.startsWith('/api/')) throw new PnlError(404, 'NOT_FOUND', 'Resource not found');
      if (!['GET', 'HEAD'].includes(request.method)) throw new PnlError(405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
      const file = resolve(publicRoot, `.${decoded === '/' ? '/index.html' : decoded}`);
      if (!file.startsWith(`${publicRoot}${sep}`)) throw new PnlError(404, 'NOT_FOUND', 'Resource not found');
      let actual; let actualRoot; let info;
      try { actualRoot = await realpath(publicRoot); actual = await realpath(file); info = await stat(actual); }
      catch (error) { if (['ENOENT', 'ENOTDIR'].includes(error.code)) throw new PnlError(404, 'NOT_FOUND', 'Resource not found'); throw error; }
      if (!actual.startsWith(`${actualRoot}${sep}`) || !info.isFile() || !MIME[extname(actual).toLowerCase()]) throw new PnlError(404, 'NOT_FOUND', 'Resource not found');
      const body = await readFile(actual);
      response.writeHead(200, { 'Content-Type': MIME[extname(actual).toLowerCase()], 'Content-Length': body.length, 'Cache-Control': 'no-cache' });
      response.end(request.method === 'HEAD' ? undefined : body);
    } catch (error) {
      if (response.headersSent || response.destroyed) { response.destroy(); return; }
      if (!(error instanceof PnlError)) console.error(JSON.stringify({ event: 'request_failed', code: error.code ?? 'INTERNAL_ERROR' }));
      if (error.status === 413) response.setHeader('Connection', 'close');
      json(response, error instanceof PnlError ? error.status : 500, { error: { code: error instanceof PnlError ? error.code : 'INTERNAL_ERROR', message: error instanceof PnlError ? error.message : 'Request failed' } });
    }
  });
  server.requestTimeout = 15_000; server.headersTimeout = 10_000; server.timeout = 20_000; server.keepAliveTimeout = 5_000; server.maxRequestsPerSocket = 100;
  server.on('clientError', (_error, socket) => { if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'); });
  return { server, ledger, researchLedger, close: async () => {
    let serverError;
    try { if (server.listening) { server.closeIdleConnections(); await new Promise((accept, reject) => server.close(error => error ? reject(error) : accept())); } } catch (error) { serverError = error; }
    const closed = await Promise.allSettled([researchLedger.close(), ledger.close()]);
    if (serverError) throw serverError;
    for (const result of closed) if (result.status === 'rejected') throw result.reason;
  } };
}

async function main() {
  const port = Number(process.env.PORT ?? 4348);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT is invalid');
  const app = await createPnlServer();
  try { await new Promise((accept, reject) => { app.server.once('error', reject); app.server.listen(port, '127.0.0.1', accept); }); }
  catch (error) { await app.close(); throw error; }
  console.log(JSON.stringify({ event: 'listening', host: '127.0.0.1', port, records: app.ledger.entries.length }));
  let shuttingDown = false;
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
    if (shuttingDown) return; shuttingDown = true;
    const deadline = setTimeout(() => process.exit(1), 25_000); deadline.unref();
    try { await app.close(); clearTimeout(deadline); process.exit(0); } catch { process.exit(1); }
  });
}
try { if (await isCliEntry(import.meta.url)) await main(); }
catch (error) { console.error(JSON.stringify({ event: 'startup_failed', message: error.message })); process.exitCode = 1; }
