import { readFile, stat } from 'node:fs/promises';
import { isCliEntry } from './lib/cli.mjs';
import { digest, validateRecord, validateResearch } from './lib/schema.mjs';

export class PublicationHttpError extends Error {
  constructor(status, remoteCode) {
    super(`Publisher failed (${status} ${remoteCode})`);
    this.status = status;
  }
}

export function publicationReceiptMatches(receipt, record) {
  if (receipt?.schemaVersion !== 1 || receipt.idempotencyKey !== record.idempotencyKey
      || receipt.asOf !== record.asOf || typeof receipt.replayed !== 'boolean') return false;
  const { sequence, acceptedAt, previousHash, payloadHash, hash } = receipt;
  return Number.isSafeInteger(sequence) && sequence > 0
    && typeof acceptedAt === 'string' && Number.isFinite(Date.parse(acceptedAt))
    && typeof previousHash === 'string' && /^[a-f0-9]{64}$/.test(previousHash)
    && payloadHash === digest(record)
    && hash === digest({ sequence, acceptedAt, previousHash, payloadHash, record });
}

function invalidReceipt() {
  return Object.assign(new Error('Read-back receipt did not match the submitted payload'), { code: 'PUBLICATION_RECEIPT_INVALID' });
}

export async function publishRecord({ baseUrl, token, record, research = false, allowLocalHttp = false, fetcher = fetch }) {
  (research ? validateResearch : validateRecord)(record);
  if (typeof token !== 'string' || token.length < 32 || token.length > 512 || /\s/.test(token)) throw new Error('TIGER_PNL_WRITE_TOKEN must contain 32-512 whitespace-free characters');
  const url = new URL(baseUrl);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('TIGER_PNL_URL must be an origin without credentials, path or query');
  if (url.protocol !== 'https:' && !(allowLocalHttp && url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname))) throw new Error('HTTPS is required; --allow-local-http permits loopback tests only');
  const request = async (path, options) => {
    const response = await fetcher(new URL(path, url), { ...options, redirect: 'error', signal: AbortSignal.timeout(20_000), headers: { Authorization: `Bearer ${token}`, ...options.headers } });
    if (!response.ok) {
      // An upstream proxy can reject the request with HTML or an empty body.
      // Keep the authoritative HTTP status even without a Tiger error envelope.
      let remoteCode = 'UNKNOWN_ERROR';
      if (response.headers.get('content-type')?.startsWith('application/json')) {
        const body = await response.json().catch(() => null);
        if (typeof body?.error?.code === 'string' && /^[A-Z0-9_]{1,80}$/.test(body.error.code)) remoteCode = body.error.code;
      } else {
        await response.body?.cancel();
      }
      throw new PublicationHttpError(response.status, remoteCode);
    }
    try { return await response.json(); }
    catch (cause) {
      if (!(cause instanceof SyntaxError)) throw cause;
      throw invalidReceipt();
    }
  };
  const key = record.idempotencyKey;
  const posted = await request(research ? '/api/v1/research' : '/api/v1/records', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(record) });
  const verified = await request(`${research ? '/api/v1/research/receipts/' : '/api/v1/receipts/'}${encodeURIComponent(key)}`, { method: 'GET' });
  if (!publicationReceiptMatches(posted?.receipt, record) || !publicationReceiptMatches(verified?.receipt, record)
      || verified.receipt.hash !== posted.receipt.hash) throw invalidReceipt();
  return verified.receipt;
}
async function main() {
  const args = process.argv.slice(2); const allowLocalHttp = args.includes('--allow-local-http'); const research = args.includes('--research'); const files = args.filter(arg => !['--allow-local-http', '--research'].includes(arg));
  if (files.length !== 1 || files[0].startsWith('--')) throw new Error('Usage: node pnl/publish.mjs /private/path/snapshot.json [--research] [--allow-local-http]');
  const info = await stat(files[0]); if (!info.isFile() || info.size > 1_048_576) throw new Error('Snapshot must be a JSON file of at most 1 MiB');
  const record = JSON.parse(await readFile(files[0], 'utf8'));
  const receipt = await publishRecord({ baseUrl: process.env.TIGER_PNL_URL ?? 'https://tigerpnl.com', token: process.env.TIGER_PNL_WRITE_TOKEN, record, research, allowLocalHttp });
  console.log(JSON.stringify({ status: 'published_and_verified', receipt }, null, 2));
}
try { if (await isCliEntry(import.meta.url)) await main(); }
catch (error) { console.error(JSON.stringify({ status: 'failed', message: error.message })); process.exitCode = 1; }
