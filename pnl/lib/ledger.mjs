import { mkdir, open, readFile, readdir, unlink, link, lstat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { PnlError, canonical, digest, validateRecord, validateResearch } from './schema.mjs';

const ZERO = '0'.repeat(64);
const filename = sequence => `${String(sequence).padStart(12, '0')}.json`;
async function syncDirectory(path) { const fd = await open(path, 'r'); try { await fd.sync(); } finally { await fd.close(); } }
export function receipt(entry, replayed = false) {
  return { schemaVersion: 1, idempotencyKey: entry.record.idempotencyKey, sequence: entry.sequence, asOf: entry.record.asOf, acceptedAt: entry.acceptedAt, hash: entry.hash, previousHash: entry.previousHash, payloadHash: entry.payloadHash, replayed };
}
export class Ledger {
  constructor(path) { this.path = resolve(path); this.recordPath = join(this.path, 'records'); this.entries = []; this.keys = new Map(); this.queue = Promise.resolve(); this.failed = false; this.closed = false; this.accepting = true; this.validate = validateRecord; }
  static async open(path) {
    const store = new this(path);
    await mkdir(store.recordPath, { recursive: true, mode: 0o700 });
    const lockPath = join(store.path, 'writer.lock');
    try {
      store.lock = await open(lockPath, 'wx', 0o600);
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      // A crash leaves the lock behind. Only a provably dead local PID is reclaimed.
      let owner;
      try { owner = JSON.parse(await readFile(lockPath, 'utf8')); } catch { throw new Error('Ledger lock is unreadable; operator review required'); }
      if (!Number.isSafeInteger(owner.pid) || owner.pid < 1) throw new Error('Ledger lock has invalid owner; operator review required');
      try { process.kill(owner.pid, 0); throw new Error('Ledger is already owned by another process'); }
      catch (probe) { if (probe.code !== 'ESRCH') throw probe; }
      await unlink(lockPath);
      store.lock = await open(lockPath, 'wx', 0o600);
    }
    try {
      await store.lock.writeFile(JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() })); await store.lock.sync(); await syncDirectory(store.path);
      await store.load();
      return store;
    } catch (error) { await store.close(); throw error; }
  }
  async load() {
    const files = (await readdir(this.recordPath)).sort();
    for (const file of files) {
      // Uncommitted temporary files are safe to remove; they were never acknowledged.
      if (/^\.pending-[a-f0-9-]+$/.test(file)) { await unlink(join(this.recordPath, file)); continue; }
      if (!/^\d{12}\.json$/.test(file)) throw new Error('Unexpected ledger file; integrity check failed');
      const path = join(this.recordPath, file); const stat = await lstat(path);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1_100_000) throw new Error('Ledger file is invalid');
      const entry = JSON.parse(await readFile(path, 'utf8'));
      const keys = ['sequence', 'acceptedAt', 'previousHash', 'payloadHash', 'record', 'hash'];
      if (!entry || Object.keys(entry).length !== keys.length || keys.some(key => !Object.hasOwn(entry, key))) throw new Error('Ledger entry shape is invalid');
      this.validate(entry.record);
      const { hash, ...body } = entry;
      if (file !== filename(this.entries.length + 1) || entry.sequence !== this.entries.length + 1 || entry.previousHash !== (this.entries.at(-1)?.hash ?? ZERO) || entry.payloadHash !== digest(entry.record) || hash !== digest(body) || !Number.isFinite(Date.parse(entry.acceptedAt))) throw new Error('Ledger hash chain integrity check failed');
      this.checkOrder(entry.record);
      if (this.keys.has(entry.record.idempotencyKey)) throw new Error('Duplicate ledger idempotency key');
      this.entries.push(entry); this.keys.set(entry.record.idempotencyKey, entry);
    }
    await syncDirectory(this.recordPath);
  }
  checkOrder(record) {
    const previous = this.entries.at(-1)?.record;
    if (!previous) {
      if (record.netExternalFlow !== 0) throw new PnlError(422, 'BASELINE_FLOW_MUST_BE_ZERO', 'First snapshot establishes baseline; netExternalFlow must be 0');
      return;
    }
    if (Date.parse(record.asOf) <= Date.parse(previous.asOf)) throw new PnlError(409, 'AS_OF_NOT_INCREASING', 'asOf must be later than the latest accepted snapshot');
    if (record.account.label !== previous.account.label) throw new PnlError(409, 'ACCOUNT_MISMATCH', 'This ledger is bound to a single account label');
    if (record.account.nav - record.netExternalFlow <= 0) throw new PnlError(422, 'INVALID_EXTERNAL_FLOW', 'NAV minus net external flow must be positive');
    const seenTrades = new Set(this.entries.flatMap(entry => entry.record.trades.map(trade => trade.id)));
    if (record.trades.some(trade => seenTrades.has(trade.id))) throw new PnlError(409, 'DUPLICATE_TRADE', 'trades must contain only executions newly disclosed in this snapshot');
    if (record.trades.some(trade => Date.parse(trade.executedAt) <= Date.parse(previous.asOf))) throw new PnlError(422, 'TRADE_OUTSIDE_PERIOD', 'New trades must be later than the previous snapshot');
  }
  append(input) {
    if (!this.accepting) return Promise.reject(new PnlError(503, 'LEDGER_UNAVAILABLE', 'Ledger is closing'));
    // Clone before queueing so a caller cannot alter a payload during an awaited write.
    this.validate(input); const record = JSON.parse(canonical(input));
    const operation = this.queue.then(async () => {
      if (this.closed || this.failed) throw new PnlError(503, 'LEDGER_UNAVAILABLE', 'Ledger is unavailable; retry using the same key after operator review');
      const existing = this.keys.get(record.idempotencyKey);
      if (existing) {
        if (existing.payloadHash !== digest(record)) throw new PnlError(409, 'IDEMPOTENCY_CONFLICT', 'This key was already used for a different payload');
        return receipt(existing, true);
      }
      this.checkOrder(record);
      const body = { sequence: this.entries.length + 1, acceptedAt: new Date().toISOString(), previousHash: this.entries.at(-1)?.hash ?? ZERO, payloadHash: digest(record), record };
      const entry = { ...body, hash: digest(body) };
      const temp = join(this.recordPath, `.pending-${randomUUID()}`); const final = join(this.recordPath, filename(entry.sequence));
      let handle;
      try {
        handle = await open(temp, 'wx', 0o600); await handle.writeFile(`${canonical(entry)}\n`); await handle.sync(); await handle.close(); handle = null;
        // link is atomic and fails on an existing destination; no accepted file is overwritten.
        await link(temp, final); await syncDirectory(this.recordPath); await unlink(temp); await syncDirectory(this.recordPath);
      } catch (error) {
        this.failed = true;
        if (handle) await handle.close();
        // The final link may already exist: fail closed; a restart verifies it before retry.
        throw new PnlError(503, 'LEDGER_WRITE_FAILED', 'Durable write failed; retry the same key after service recovery');
      }
      this.entries.push(entry); this.keys.set(record.idempotencyKey, entry);
      return receipt(entry);
    });
    this.queue = operation.catch(() => undefined);
    return operation;
  }
  getReceipt(key) { const entry = this.keys.get(key); return entry ? receipt(entry) : null; }
  async close() {
    this.accepting = false;
    await this.queue; this.closed = true;
    if (this.lock) { await this.lock.close(); this.lock = null; await unlink(join(this.path, 'writer.lock')); await syncDirectory(this.path); }
  }
}

export class ResearchLedger extends Ledger {
  constructor(path) { super(path); this.validate = validateResearch; }
  append(input) {
    // Old immutable records remain readable. A historical retry cannot create a new entry.
    if (!this.keys.has(input?.idempotencyKey) && (input?.schemaVersion !== 2 || input?.modelMode !== 'enabled')) return Promise.reject(new PnlError(422, 'MODEL_REQUIRED', 'New execution decisions require the verified DeepSeek model contract'));
    return super.append(input);
  }
  checkOrder(record) {
    const previous = this.entries.at(-1)?.record;
    if (previous && Date.parse(record.asOf) <= Date.parse(previous.asOf)) throw new PnlError(409, 'AS_OF_NOT_INCREASING', 'Research asOf must be later than the latest accepted research');
    if (this.entries.some(entry => entry.record.runId === record.runId)) throw new PnlError(409, 'DUPLICATE_RESEARCH_RUN', 'A research run may only be disclosed once; retry its original key');
  }
}
