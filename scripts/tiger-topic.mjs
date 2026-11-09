#!/usr/bin/env node
/** Typed topic transport. Startup capabilities never come from tool arguments. */
import { spawn } from 'node:child_process';
import { isAbsolute, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseJson, readRequestInput, runtimeEnvironment } from '../bundles/trading/capabilities/workflow-client.mjs';

export const ROOT = resolve(import.meta.dirname, '..');
export const OPERATIONS = ['create', 'research', 'watch', 'status', 'report', 'evidence', 'health', 'tick'];
export const WRITE = new Set(['create', 'research', 'watch', 'tick']);
export const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
export class TopicError extends Error { constructor(code) { super(code); this.code = code; } }
export function failure(operation, code) { return { schema: 'tiger.topic-operation/v1', operation, operation_ok: false, data: {}, error: { code }, limitations: [] }; }
export function exitCode(result) {
  if (result.operation_ok) return 0;
  const code = result.error?.code;
  return ['MODE_DENIED', 'ACCESS_DENIED', 'POLICY_DENIED'].includes(code) ? 3
    : ['IDEMPOTENCY_CONFLICT', 'QUEUE_FULL'].includes(code) ? 5
      : ['INVALID_REQUEST', 'INVALID_STARTUP', 'INPUT_TOO_LARGE', 'CONTRACT_INVALID'].includes(code) ? 2
        : code === 'WALL_DEADLINE' ? 9 : 7;
}
export function startupOptions(options) {
  const value = { mode: 'readonly', python: 'python3', tenant: 'public', ...options };
  if (Object.keys(value).some(k => !['mode', 'python', 'tenant', 'stateRoot', 'archiveRoot', 'policyFile'].includes(k))
    || !['readonly', 'acquire', 'research'].includes(value.mode) || typeof value.python !== 'string'
    || !value.python || value.python.startsWith('-') || /[\x00-\x1f]/u.test(value.python)
    || !isAbsolute(value.stateRoot ?? '') || !isAbsolute(value.archiveRoot ?? '')
    || value.policyFile && !isAbsolute(value.policyFile) || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u.test(value.tenant)) throw new TopicError('INVALID_STARTUP');
  return value;
}
export function permitted(operation, mode) {
  if (operation === 'research') return mode === 'research';
  return !WRITE.has(operation) || mode !== 'readonly';
}
export function parseArgs(argv, mcp = false) {
  const operation = mcp ? null : argv[0];
  if (!mcp && !OPERATIONS.includes(operation)) throw new TopicError('INVALID_STARTUP');
  const mapped = { '--mode': 'mode', '--python': 'python', '--state-root': 'stateRoot', '--archive-root': 'archiveRoot', '--policy-file': 'policyFile', '--tenant': 'tenant' };
  const options = {};
  let file = '-', wait = false, waitSeconds = 2520;
  for (let i = mcp ? 0 : 1; i < argv.length; i++) {
    const flag = argv[i];
    if (!mcp && flag === '--wait' && !wait) { wait = true; continue; }
    const value = argv[++i];
    if (!value || value.startsWith('--') || /[\x00-\x1f]/u.test(value)) throw new TopicError('INVALID_STARTUP');
    if (!mcp && flag === '--input-file' && file === '-') file = value;
    else if (!mcp && flag === '--wait-seconds' && /^[1-9][0-9]*$/u.test(value) && Number(value) <= 86400) waitSeconds = Number(value);
    else if (mapped[flag] && !Object.hasOwn(options, mapped[flag])) options[mapped[flag]] = value;
    else throw new TopicError('INVALID_STARTUP');
  }
  if (wait && operation !== 'research') throw new TopicError('INVALID_STARTUP');
  return { operation, config: startupOptions(options), file, wait, waitSeconds };
}
export function topicEnvironment(mode, environment = process.env) {
  const result = runtimeEnvironment(environment, mode === 'research');
  for (const name of ['TIGER_INTEL_REGISTRY_FILE', 'TIGER_INTEL_CATALOG_FILE']) if (typeof environment[name] === 'string') result[name] = environment[name];
  if (mode !== 'readonly') for (const name of ['LLM_API_KEY', 'TIGER_INTEL_REGISTRY_FILE', 'TIGER_INTEL_CATALOG_FILE', 'TIGER_INTEL_SEC_USER_AGENT', 'TIGER_INTEL_BRAVE_API_KEY', 'TIGER_INTEL_TAVILY_API_KEY', 'TIGER_INTEL_EXA_API_KEY', 'TIGER_INTEL_FRED_API_KEY']) if (typeof environment[name] === 'string') result[name] = environment[name];
  return result;
}
export async function invoke(operation, input, options, { signal, spawnImpl = spawn } = {}) {
  const config = startupOptions(options);
  if (!OPERATIONS.includes(operation) || !plain(input)) throw new TopicError('INVALID_REQUEST');
  if (!permitted(operation, config.mode)) throw new TopicError('MODE_DENIED');
  if (signal?.aborted) throw new TopicError('INTERRUPTED');
  const args = ['-B', '-m', 'trading_system.intelligence.topic_cli', operation, '--state-root', config.stateRoot,
    '--archive-root', config.archiveRoot, '--mode', config.mode, '--tenant', config.tenant,
    ...(config.policyFile ? ['--policy-file', config.policyFile] : [])];
  return await new Promise((done, reject) => {
    let child, timer, killTimer, cleanupTimer, failed, finished = false, bytes = 0, diagnostics = 0;
    const chunks = [];
    const kill = kind => { try { if (child?.pid) process.kill(-child.pid, kind); } catch (error) { if (error.code !== 'ESRCH') failed ??= new TopicError('RUNNER_FAILED'); } };
    const finish = (error, result) => { if (finished) return; finished = true; clearTimeout(timer); clearTimeout(killTimer); clearTimeout(cleanupTimer); signal?.removeEventListener('abort', abort); if (error) reject(error); else done(result); };
    const stop = code => { if (failed || finished) return; failed = new TopicError(code); kill('SIGTERM'); killTimer = setTimeout(() => kill('SIGKILL'), 250); cleanupTimer = setTimeout(() => { kill('SIGKILL'); child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy(); finish(failed); }, 2000); };
    const abort = () => stop('INTERRUPTED');
    try { child = spawnImpl(config.python, args, { cwd: ROOT, detached: true, shell: false, env: topicEnvironment(WRITE.has(operation) ? config.mode : 'readonly'), stdio: ['pipe', 'pipe', 'pipe'] }); }
    catch { finish(new TopicError('RUNNER_FAILED')); return; }
    child.on('error', () => finish(new TopicError('RUNNER_FAILED')));
    child.stdout.on('data', data => { bytes += data.length; if (bytes > 8 * 1024 * 1024) stop('OUTPUT_TOO_LARGE'); else if (!failed) chunks.push(data); });
    child.stderr.on('data', data => { diagnostics += data.length; if (diagnostics > 65536) stop('RUNNER_FAILED'); });
    child.stdin.on('error', error => { if (error.code !== 'EPIPE') stop('RUNNER_FAILED'); });
    child.on('close', (code, exitSignal) => {
      kill('SIGKILL');
      if (failed) { finish(failed); return; }
      try {
        const value = parseJson(Buffer.concat(chunks), 8 * 1024 * 1024);
        if (value.schema !== 'tiger.topic-operation/v1' || value.operation !== operation || typeof value.operation_ok !== 'boolean'
          || !plain(value.data) || !Array.isArray(value.limitations) || value.limitations.some(x => typeof x !== 'string')
          || value.operation_ok !== (value.error === null) || !value.operation_ok && !/^[A-Z][A-Z0-9_]{1,79}$/u.test(value.error?.code ?? '')
          || exitSignal || code !== exitCode(value)) throw new TopicError('RESULT_INVALID');
        if (operation === 'report' && value.operation_ok) {
          const view = value.data, report = view.report, current = view.current_source_status;
          if (view.schema !== 'tiger.topic-report-view/v1' || !plain(report) || report.schema !== 'tiger.topic-report/v1'
            || !['completed', 'partial'].includes(report.state) || !/^[a-f0-9]{64}$/u.test(view.sealed_receipt_sha256 ?? '')
            || view.sealed_receipt_sha256 !== report.receipt_sha256 || !plain(current)
            || current.schema !== 'tiger.topic-source-status/v1' || current.network_checked !== false
            || !['unknown', 'no_known_warning', 'needs_review'].includes(current.state) || !Array.isArray(current.warnings)) throw new TopicError('RESULT_INVALID');
        }
        finish(null, value);
      } catch { finish(new TopicError('RESULT_INVALID')); }
    });
    timer = setTimeout(() => stop('WALL_DEADLINE'), operation === 'tick' ? 300000 : 60000);
    signal?.addEventListener('abort', abort, { once: true });
    child.stdin.end(JSON.stringify(input) + '\n');
    if (signal?.aborted) abort();
  });
}
export async function main(argv = process.argv.slice(2)) {
  const controller = new AbortController(); const abort = () => controller.abort();
  process.once('SIGINT', abort); process.once('SIGTERM', abort);
  let result, operation = OPERATIONS.includes(argv[0]) ? argv[0] : null;
  try {
    const parsed = parseArgs(argv); operation = parsed.operation;
    if (!permitted(operation, parsed.config.mode)) throw new TopicError('MODE_DENIED');
    const input = await readRequestInput(parsed.file, process.stdin, { signal: controller.signal });
    result = await invoke(operation, input, parsed.config, { signal: controller.signal });
    if (parsed.wait && result.operation_ok) {
      const deadline = Date.now() + parsed.waitSeconds * 1000, job = result.data.job_id;
      while (Date.now() < deadline && !controller.signal.aborted) {
        const status = await invoke('status', { job_id: job }, parsed.config, { signal: controller.signal });
        if (!status.operation_ok || ['completed', 'partial', 'failed', 'blocked', 'interrupted'].includes(status.data.state)) { result = status; break; }
        await new Promise(resolveWait => {
          const settle = () => { clearTimeout(timer); controller.signal.removeEventListener('abort', settle); resolveWait(); };
          const timer = setTimeout(settle, 1000); controller.signal.addEventListener('abort', settle, { once: true });
          if (controller.signal.aborted) settle();
        });
      }
    }
  } catch (error) { result = failure(operation, error?.code ?? 'RUNNER_FAILED'); }
  process.removeListener('SIGINT', abort); process.removeListener('SIGTERM', abort);
  try { await new Promise((done, reject) => process.stdout.write(JSON.stringify(result) + '\n', error => error ? reject(error) : done())); }
  catch { process.exitCode = 130; return result; }
  process.exitCode = exitCode(result); return result;
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
