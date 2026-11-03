import { spawn } from 'node:child_process';
import { mkdir, open, readFile, realpath, rename, stat, unlink } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, isAbsolute, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { exitCode, parseJson, runtimeEnvironment, safeEvidencePath, sanitizeMessage, validateEnvelope, validateRequest } from '../bundles/trading/capabilities/workflow-client.mjs';
import { canonical, MODEL_PROFILE, validateResearch } from './lib/schema.mjs';
import { isCliEntry } from './lib/cli.mjs';
import { PublicationHttpError, publicationReceiptMatches, publishRecord } from './publish.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PROFILES = new Set([MODEL_PROFILE]);
const RUN_ID = /^run-[0-9a-f]{32}$/;
const COMPLETE = new Set(['completed']);
const sha256 = value => createHash('sha256').update(value).digest('hex');
export class SyncError extends Error { constructor(code, message) { super(message); this.code = code; } }
const error = (code, message) => { throw new SyncError(code, message); };
async function syncDir(path) { const fd = await open(path, 'r'); try { await fd.sync(); } finally { await fd.close(); } }
async function saveState(file, value, env) {
  const serialized = canonical(value);
  for (const [name, secret] of Object.entries(env)) {
    if (/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(name) && typeof secret === 'string' && secret.length >= 4 && serialized.includes(secret)) error('STATE_SECRET_REJECTED', 'Sync state unexpectedly contains a protected credential and was not persisted');
  }
  const pending = `${file}.pending-${randomUUID()}`; const fd = await open(pending, 'wx', 0o600);
  try { await fd.writeFile(`${serialized}\n`); await fd.sync(); } finally { await fd.close(); }
  await rename(pending, file); await syncDir(dirname(file));
}
async function acquireLock(path) {
  try { return await open(path, 'wx', 0o600); }
  catch (cause) {
    if (cause.code !== 'EEXIST') throw cause;
    let owner; let original;
    try { original = await stat(path); owner = JSON.parse(await readFile(path, 'utf8')); }
    catch { error('SYNC_BUSY', 'Sync lock is being initialized or requires protected operator review'); }
    if (!Number.isSafeInteger(owner.pid) || owner.pid < 1) error('SYNC_BUSY', 'Sync lock owner is invalid');
    try { process.kill(owner.pid, 0); error('SYNC_BUSY', 'This date/profile already has a live sync observer'); }
    catch (probe) { if (probe.code !== 'ESRCH') throw probe; }
    const current = await stat(path);
    if (current.ino !== original.ino || current.dev !== original.dev) error('SYNC_BUSY', 'Sync lock changed during stale-owner recovery');
    await unlink(path);
    try { return await open(path, 'wx', 0o600); } catch (retry) { if (retry.code === 'EEXIST') error('SYNC_BUSY', 'Another observer acquired the recovered sync lock'); throw retry; }
  }
}
function validDate(value) { return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value; }
function marketDate() { return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()); }

/** Invoke only the existing controlled Node CLI; no free Python or trading commands. */
export function invokeAgent(operation, input, config, timeoutMs) {
  validateRequest(operation, input);
  return new Promise((accept, reject) => {
    const args = [resolve(ROOT, 'scripts/tiger-agent.mjs'), operation, '--python', config.python, '--state-root', config.stateRoot, '--mode', 'paper', '--input-file', '-'];
    const child = spawn(process.execPath, args, { cwd: ROOT, env: runtimeEnvironment(config.env, operation === 'run' && input.profile === MODEL_PROFILE), stdio: ['pipe', 'pipe', 'pipe'] });
    const chunks = []; let stdoutBytes = 0; let stderrBytes = 0; let failure; let forced;
    const stop = value => { failure ??= value; child.kill('SIGTERM'); forced ??= setTimeout(() => child.kill('SIGKILL'), 750); };
    const timer = setTimeout(() => stop(new SyncError('OBSERVATION_TIMEOUT', 'Observation stopped; accepted workers continue independently')), Math.max(1, timeoutMs));
    child.stdout.on('data', chunk => { stdoutBytes += chunk.length; if (stdoutBytes > 1_048_576) stop(new SyncError('RESULT_INVALID', 'Controlled CLI output exceeded limit')); else chunks.push(chunk); });
    child.stderr.on('data', chunk => { stderrBytes += chunk.length; if (stderrBytes > 65536) stop(new SyncError('RESULT_INVALID', 'Controlled CLI diagnostics exceeded limit')); });
    child.stdin.on('error', cause => { if (cause.code !== 'EPIPE') stop(new SyncError('RUNNER_FAILED', 'Unable to deliver controlled request')); });
    child.on('error', () => { clearTimeout(timer); clearTimeout(forced); reject(new SyncError('RUNNER_FAILED', 'Unable to start the controlled Tiger CLI')); });
    child.on('close', code => {
      clearTimeout(timer); clearTimeout(forced);
      if (failure) { reject(failure); return; }
      try { const envelope = validateEnvelope(parseJson(Buffer.concat(chunks), 1_048_576), operation, input); if (code !== exitCode(envelope)) error('RESULT_INVALID', 'Controlled CLI exit code disagrees with its envelope'); accept(envelope); }
      catch { reject(new SyncError('RESULT_INVALID', 'Controlled CLI did not return a valid envelope')); }
    });
    child.stdin.end(`${JSON.stringify(input)}\n`);
  });
}

async function evidenceFile(stateRoot, runId, name, refs) {
  const matches = refs.filter(ref => ref.path === name); const reference = matches[0];
  if (matches.length !== 1 || !reference || !safeEvidencePath(reference.path) || !/^[a-f0-9]{64}$/.test(reference.sha256)) error('EVIDENCE_MISSING', 'Required unique verified artifact reference is missing');
  const trustedRoot = await realpath(stateRoot); const root = await realpath(resolve(trustedRoot, 'runs', runId));
  if (!root.startsWith(`${trustedRoot}${sep}runs${sep}`)) error('EVIDENCE_INVALID', 'Run artifact escapes the trusted state root');
  const file = await realpath(resolve(root, name));
  if (!file.startsWith(`${root}${sep}`)) error('EVIDENCE_INVALID', 'Verified artifact escapes its run directory');
  const info = await stat(file);
  if (!info.isFile() || info.size !== reference.bytes || info.size > 1_048_576) error('EVIDENCE_INVALID', 'Verified artifact size differs');
  const bytes = await readFile(file);
  if (sha256(bytes) !== reference.sha256) error('EVIDENCE_HASH_MISMATCH', 'Verified artifact hash differs');
  return parseJson(bytes, 1_048_576);
}

export async function buildResearch({ receipt, verification, stateRoot, profile, idempotencyKey, expectedRequest = null }) {
  if (profile !== MODEL_PROFILE) error('MODEL_REQUIRED', 'New publication requires the reviewed model-required profile');
  if (!receipt?.run_id || !RUN_ID.test(receipt.run_id)) error('EVIDENCE_INVALID', 'Verified run identity is missing');
  const runId = receipt.run_id;
  validateEnvelope(receipt, 'receipt', { run_id: runId }); validateEnvelope(verification, 'verify', { run_id: runId });
  if (!receipt.operation_ok || !verification.operation_ok || !COMPLETE.has(receipt.status) || receipt.status !== verification.status || receipt.verification?.ok !== true || verification.verification?.ok !== true || receipt.verification.receipt_sha256 !== verification.verification.receipt_sha256) error('EVIDENCE_INVALID', 'Receipt and independent verification must both pass for the same completed run');
  const raw = await evidenceFile(stateRoot, runId, 'receipt.json', verification.evidence_refs);
  if (raw.schema !== 'tiger.receipt.v1' || raw.run_id !== runId || raw.profile !== profile || raw.paper_account !== 'public-usd' || raw.status !== receipt.status) error('EVIDENCE_INVALID', 'Verified original receipt identity differs');
  const original = await evidenceFile(stateRoot, runId, 'request.json', verification.evidence_refs);
  validateRequest('run', original);
  if (original.profile !== profile || (expectedRequest && canonical(original) !== canonical(expectedRequest))) error('EVIDENCE_INVALID', 'Verified original request differs from the persistent request');
  const receiptRef = verification.evidence_refs.find(ref => ref.path === 'receipt.json');
  if (receiptRef.sha256 !== receipt.verification.receipt_sha256) error('EVIDENCE_HASH_MISMATCH', 'Original receipt hash differs from the independent verification');
  const summary = receipt.summary; const disclosures = summary?.disclosures;
  if (!summary || canonical(summary) !== canonical(raw.summary)) error('EVIDENCE_INVALID', 'Displayed summary differs from the hash-bound original receipt');
  if (disclosures?.synthetic !== false || disclosures.execution_mode !== 'paper' || disclosures.model_mode !== 'enabled' || disclosures.model_required !== true) error('MODEL_REQUIRED', 'A genuine required model assessment is mandatory; disabled and passthrough runs cannot be published');
  if (!Array.isArray(summary.picks) || !Array.isArray(summary.watchlist)) error('EVIDENCE_INVALID', 'Verified research recommendations are missing');
  const stamp = Date.parse(raw.sealed_at_utc);
  if (!Number.isFinite(stamp)) error('EVIDENCE_INVALID', 'Verified completion timestamp is missing');
  if (!Array.isArray(summary.execution_decisions)) error('MODEL_REQUIRED', 'Verified model execution decisions are missing');
  const asOf = new Date(stamp).toISOString();
  const decisions = summary.execution_decisions.map(item => ({ id: `${runId}-${item.action}-${item.symbol}`, symbol: item.symbol, action: item.action, targetWeight: item.target_weight, thesis: item.action_summary, decidedAt: asOf, runId }));
  const universeSize = disclosures.scan_stats?.universe_size;
  if (!Number.isSafeInteger(universeSize) || universeSize < 0) error('EVIDENCE_INVALID', 'Verified research universe size is missing');
  const record = { schemaVersion: 2, idempotencyKey, asOf, runId, receiptHash: receipt.verification.receipt_sha256, dataAsOf: summary.as_of?.benchmark_last_bar, executionMode: 'paper', modelMode: 'enabled', status: receipt.status, universeSize, summary: summary.action_summary, decisions, model: summary.model, executionDecisions: summary.execution_decisions };
  validateResearch(record);
  return { record, originalRequest: original, evidence: { runId, receiptHash: record.receiptHash, sourceTreeHash: verification.data?.source?.tree_sha256 ?? null, status: record.status, evidenceCount: verification.verification.evidence_count, checks: verification.verification.checks } };
}

export async function syncResearch({ env = process.env, date = marketDate(), profile = env.TIGER_PNL_RESEARCH_PROFILE ?? MODEL_PROFILE, runId = null, observeMs = 50000, allowLocalHttp = false } = {}, { invoke = invokeAgent, publish = publishRecord } = {}) {
  if (!PROFILES.has(profile) || !validDate(date) || (runId !== null && !RUN_ID.test(runId))) error('INVALID_SYNC_REQUEST', 'Use a reviewed profile, ISO date and valid optional run ID');
  if (env.TIGER_AGENT_PERMISSION !== 'paper' || !isAbsolute(env.TIGER_AGENT_PYTHON ?? '') || !isAbsolute(env.TIGER_AGENT_STATE_ROOT ?? '') || !isAbsolute(env.TIGER_PNL_SYNC_STATE_DIR ?? '')) error('INVALID_TRUSTED_CONFIGURATION', 'Trusted paper permission and absolute Python, agent-state and sync-state paths are required');
  if (typeof env.TIGER_PNL_WRITE_TOKEN !== 'string' || env.TIGER_PNL_WRITE_TOKEN.length < 32 || env.TIGER_PNL_WRITE_TOKEN.length > 512 || /\s/.test(env.TIGER_PNL_WRITE_TOKEN)) error('INVALID_TRUSTED_CONFIGURATION', 'A protected valid website write token is required');
  if (!Number.isInteger(observeMs) || observeMs < 1 || observeMs > 50000) error('INVALID_SYNC_REQUEST', 'Observation must be 1-50000 milliseconds');
  const config = { python: env.TIGER_AGENT_PYTHON, stateRoot: resolve(env.TIGER_AGENT_STATE_ROOT), env };
  await mkdir(config.stateRoot, { recursive: true, mode: 0o700 }); config.stateRoot = await realpath(config.stateRoot);
  let directory = resolve(env.TIGER_PNL_SYNC_STATE_DIR); await mkdir(directory, { recursive: true, mode: 0o700 }); directory = await realpath(directory);
  const source = await realpath(ROOT);
  if (directory === source || directory.startsWith(`${source}${sep}`)) error('INVALID_TRUSTED_CONFIGURATION', 'Sync state must be outside the source tree');
  const identity = `${profile}-${date}`; const file = resolve(directory, `${identity}.json`); const lockPath = `${file}.lock`; const lock = await acquireLock(lockPath);
  try {
    await lock.writeFile(JSON.stringify({ pid: process.pid })); await lock.sync();
    let state;
    try { const info = await stat(file); if (!info.isFile() || info.size > 1_100_000) error('SYNC_STATE_INVALID', 'Persistent sync state is invalid'); state = JSON.parse(await readFile(file, 'utf8')); }
    catch (cause) { if (cause.code !== 'ENOENT') throw cause; }
    const request = { schema_version: 'tiger.agent-request/v1', profile, paper_account: 'public-usd', idempotency_key: `tigerpnl-research-${identity}` };
    if (state) {
      if (state.schemaVersion !== 1 || state.profile !== profile || state.date !== date || state.runnerStateRoot !== config.stateRoot || state.python !== config.python || (runId && state.runId !== runId) || (state.runId !== null && !RUN_ID.test(state.runId)) || !['prepared', 'accepted', 'verified', 'published', 'failed'].includes(state.phase)) error('SYNC_STATE_CONFLICT', 'Persistent date/profile identity or trusted configuration differs');
      if (!state.imported && canonical(state.request) !== canonical(request)) error('SYNC_STATE_CONFLICT', 'Persistent original request differs');
      if (state.payload) { validateResearch(state.payload); if (state.payload.runId !== state.runId || state.payload.idempotencyKey !== `pnl-${identity}`) error('SYNC_STATE_CONFLICT', 'Persistent publication identity differs'); }
      if (['verified', 'published'].includes(state.phase) && (!state.payload || state.evidence?.receiptHash !== state.payload.receiptHash || state.evidence?.runId !== state.runId)) error('SYNC_STATE_CONFLICT', 'Persistent verified evidence is missing or differs');
      if (state.phase === 'published' && !publicationReceiptMatches(state.websiteReceipt, state.payload)) error('SYNC_STATE_CONFLICT', 'Persistent published receipt is missing or differs');
    } else {
      state = { schemaVersion: 1, date, profile, python: config.python, runnerStateRoot: config.stateRoot, request: runId ? null : request, imported: Boolean(runId), runId, phase: 'prepared', payload: null, evidence: null, websiteReceipt: null };
      await saveState(file, state, env);
    }
    if (state.phase === 'published') return { status: 'already_published', runId: state.runId, receipt: state.websiteReceipt };
    const deadline = Date.now() + observeMs;
    const pending = async reason => { await saveState(file, state, env); return { status: 'pending', reason, phase: state.phase, runId: state.runId, idempotencyKey: state.request?.idempotency_key ?? null, observationStopped: true, workerCancellationRequested: false }; };
    const call = async (operation, input) => { const remaining = deadline - Date.now(); if (remaining < 1000) error('OBSERVATION_TIMEOUT', 'Observation stopped; accepted workers continue independently'); return invoke(operation, input, config, Math.max(1, remaining - 500)); };
    try {
      const doctor = await call('doctor', {});
      if (!doctor.operation_ok || doctor.status !== 'ready' || doctor.data?.access_mode !== 'paper') error('RUNNER_NOT_READY', 'Controlled Tiger doctor did not report a ready paper deployment');
      const description = await call('describe', { profile });
      if (!description.operation_ok || description.data?.profile?.profile !== profile || description.data?.access_mode !== 'paper') error('PROFILE_NOT_AVAILABLE', 'Controlled Tiger describe did not match the reviewed profile');
      const modelContract = description.data.profile.model_contract;
      if (description.data.profile.execution?.model_mode !== 'enabled' || modelContract?.required !== true || modelContract.provider !== 'deepseek' || modelContract.model !== 'deepseek-flash' || modelContract.release !== 'DeepSeek-V4.1-Flash-1017' || modelContract.search_mode !== 'provider_native_search') error('MODEL_REQUIRED', 'Controlled Tiger describe did not advertise the exact required DeepSeek policy');
      if (!state.runId) {
        if (env.LLM_BACKEND !== 'api' || env.LLM_BASE_URL !== 'https://api.deepseek.com' || env.LLM_MODEL !== 'deepseek-flash' || typeof env.LLM_API_KEY !== 'string' || !env.LLM_API_KEY.length || env.LLM_API_KEY.length > 4096 || /[^\x21-\x7e]/.test(env.LLM_API_KEY)) error('MODEL_CONFIGURATION_REQUIRED', 'Load the protected fixed DeepSeek configuration before starting a new run');
        const accepted = await call('run', state.request);
        if (accepted.run_id && RUN_ID.test(accepted.run_id)) { state.runId = accepted.run_id; state.phase = 'accepted'; await saveState(file, state, env); }
        if (!accepted.operation_ok) {
          if (['TIMEOUT', 'INTERRUPTED', 'CANCELLED', 'ACCOUNT_BUSY'].includes(accepted.error?.code)) return pending(accepted.error.code);
          error(accepted.error?.code ?? 'RUNNER_FAILED', 'Controlled Tiger did not accept the persistent request');
        }
        if (!state.runId) error('RESULT_INVALID', 'Controlled Tiger accepted without a run identity');
      }
      while (!state.payload) {
        const observed = await call('status', { run_id: state.runId });
        if (observed.run_id !== state.runId || !observed.operation_ok) error(observed.error?.code ?? 'RESULT_INVALID', 'Unable to inspect the existing run identity');
        if (COMPLETE.has(observed.status)) {
          const receipt = await call('receipt', { run_id: state.runId });
          const verification = await call('verify', { run_id: state.runId });
          const built = await buildResearch({ receipt, verification, stateRoot: config.stateRoot, profile, idempotencyKey: `pnl-${identity}`, expectedRequest: state.request });
          state.payload = built.record; state.evidence = built.evidence; state.request = built.originalRequest; state.phase = 'verified'; await saveState(file, state, env); break;
        }
        if (!['accepted', 'running'].includes(observed.status)) { state.phase = 'failed'; await saveState(file, state, env); error('RUN_NOT_COMPLETED', 'Existing research run failed; its original identity is retained and will not be replaced'); }
        if (deadline - Date.now() < 2000) return pending('OBSERVATION_TIMEOUT');
        await new Promise(accept => setTimeout(accept, Math.min(1000, Math.max(1, deadline - Date.now() - 1000))));
      }
      const fetcher = (url, options) => { const remaining = deadline - Date.now(); if (remaining < 1) error('OBSERVATION_TIMEOUT', 'Publication observation deadline reached'); return fetch(url, { ...options, signal: AbortSignal.timeout(Math.min(20000, remaining)) }); };
      // Payload is persisted before network submission, preserving exact retries after lost responses.
      const written = await publish({ baseUrl: env.TIGER_PNL_URL ?? 'https://tigerpnl.com', token: env.TIGER_PNL_WRITE_TOKEN, record: state.payload, research: true, allowLocalHttp, fetcher });
      if (!publicationReceiptMatches(written, state.payload)) error('PUBLICATION_RECEIPT_INVALID', 'Website receipt does not match the persistent research payload');
      // Preserve only the narrow receipt, never arbitrary remote response fields.
      state.websiteReceipt = Object.fromEntries(['schemaVersion', 'idempotencyKey', 'sequence', 'asOf', 'acceptedAt', 'hash', 'previousHash', 'payloadHash', 'replayed'].filter(key => Object.hasOwn(written, key)).map(key => [key, written[key]]));
      state.phase = 'published'; await saveState(file, state, env);
      return { status: 'published_and_verified', runId: state.runId, receipt: state.websiteReceipt, research: { asOf: state.payload.asOf, dataAsOf: state.payload.dataAsOf, decisions: state.payload.decisions.length } };
    } catch (cause) {
      if (['OBSERVATION_TIMEOUT', 'TIMEOUT', 'INTERRUPTED', 'CANCELLED', 'AbortError', 'TimeoutError'].includes(cause.code ?? cause.name)) return pending(cause.code ?? cause.name);
      if (cause instanceof PublicationHttpError && cause.status >= 400 && cause.status < 500 && ![408, 425, 429].includes(cause.status)) {
        error('PUBLICATION_REJECTED', `Website rejected the persistent research payload. ${cause.message}. The original identity is retained`);
      }
      if (cause.code === 'PUBLICATION_RECEIPT_INVALID') error(cause.code, cause.message);
      if (state.phase === 'verified' && !(cause instanceof SyncError)) return pending('PUBLICATION_RETRY_REQUIRED');
      throw cause;
    }
  } finally { await lock.close(); await unlink(lockPath); await syncDir(directory); }
}

export function parseArgs(args) {
  const options = {}; const allowed = new Map([['--date', 'date'], ['--profile', 'profile'], ['--run-id', 'runId']]);
  for (let i = 0; i < args.length; i++) { const name = allowed.get(args[i]); if (!name || Object.hasOwn(options, name) || !args[i + 1] || args[i + 1].startsWith('--')) error('INVALID_SYNC_REQUEST', 'Use only --date, --profile and optional --run-id once each'); options[name] = args[++i]; }
  return options;
}
try { if (await isCliEntry(import.meta.url)) console.log(JSON.stringify(await syncResearch(parseArgs(process.argv.slice(2))))); }
catch (cause) { console.error(JSON.stringify({ status: 'failed', code: cause.code ?? 'SYNC_FAILED', message: sanitizeMessage(cause.message) })); process.exitCode = 1; }
