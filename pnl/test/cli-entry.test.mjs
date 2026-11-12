import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPOSITORY = fileURLToPath(new URL('../../', import.meta.url));
const ENTRIES = [
  ['backup.mjs', 'backupLedger'], ['publish.mjs', 'publishRecord'],
  ['sync-research.mjs', 'syncResearch'], ['server.mjs', 'createPnlServer'],
];
function environment() {
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('TIGER_') && key !== 'NODE_TEST_CONTEXT'));
  // Any unexpected main invocation must fail before touching a real account or network.
  env.PORT = '0';
  return env;
}
async function temporary(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-cli-entry-test-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}
function run(args, cwd) {
  const result = spawnSync(process.execPath, args, { cwd, env: environment(), encoding: 'utf8', timeout: 15_000 });
  assert.equal(result.error, undefined); assert.equal(result.signal, null);
  return result;
}
function failure(result, entry, entryResolutionFailure = false) {
  assert.equal(result.status, 1, `${entry} must execute its CLI validation instead of silently exiting0`);
  assert.equal(result.stdout, '');
  const records = result.stderr.trim().split('\n').map(line => JSON.parse(line));
  // An unexpected entry-path fault can be observed by an imported CLI module
  // before its importing CLI. Every diagnostic must stay structured and failed.
  for (const record of records) assert.ok(record.status === 'failed' || record.event === 'startup_failed');
  if (!entryResolutionFailure) assert.equal(records.length, 1);
  const record = records.at(-1);
  if (entry === 'server.mjs') assert.equal(record.event, 'startup_failed');
  else assert.equal(record.status, 'failed');
  return record;
}

for (const [entry] of ENTRIES) {
  for (const kind of ['directory', 'file']) {
    test(`${entry} validates a CLI request through a ${kind} symlink`, async t => {
      const path = await temporary(t); const link = join(path, kind === 'directory' ? 'current' : entry);
      await symlink(kind === 'directory' ? REPOSITORY : join(REPOSITORY, 'pnl', entry), link);
      const cli = kind === 'directory' ? join(link, 'pnl', entry) : link;
      const record = failure(run([cli, '--invalid'], path), entry);
      if (entry === 'server.mjs') assert.equal(record.message, 'PORT is invalid');
      if (entry === 'publish.mjs') assert.match(record.message, /Usage:/);
      if (entry === 'sync-research.mjs') assert.equal(record.code, 'INVALID_SYNC_REQUEST');
      if (entry === 'backup.mjs') assert.equal(record.code, 'BACKUP_VERIFICATION_FAILED');
    });
  }
}

for (const scenario of ['absent argv', 'another caller', 'missing caller', 'nondirectory caller']) {
  test(`all four library imports with ${scenario} remain side-effect free`, async t => {
    const path = await temporary(t); const caller = join(path, 'caller.mjs');
    await writeFile(caller, '// Local import caller fixture only.\n');
    const argv = scenario === 'absent argv' ? 'process.argv.splice(1);' :
      `process.argv[1] = ${JSON.stringify(scenario === 'another caller' ? caller : scenario === 'missing caller' ? join(path, 'missing.mjs') : join(caller, 'not-a-directory.mjs'))};`;
    const imports = ENTRIES.map(([entry, exported]) => [pathToFileURL(join(REPOSITORY, 'pnl', entry)).href, exported]);
    const code = `${argv}\nfor (const [url, exported] of ${JSON.stringify(imports)}) { const module = await import(url); if (typeof module[exported] !== 'function') throw new Error('Expected actual library export'); }\nconsole.log(JSON.stringify({imported:4}));`;
    const before = await readdir(path); const result = run(['--input-type=module', '--eval', code], path);
    assert.equal(result.status, 0, result.stderr); assert.equal(result.stderr, '');
    assert.deepEqual(JSON.parse(result.stdout), { imported: 4 });
    assert.deepEqual(await readdir(path), before);
  });
}

for (const [entry] of ENTRIES) {
  test(`${entry} reports unexpected entry-path failures as structured nonzero errors`, async t => {
    const path = await temporary(t); const loop = join(path, 'entry-loop');
    await symlink('entry-loop', loop);
    const code = `process.argv[1] = ${JSON.stringify(loop)}; await import(${JSON.stringify(pathToFileURL(join(REPOSITORY, 'pnl', entry)).href)});`;
    const record = failure(run(['--input-type=module', '--eval', code], path), entry, true);
    if (entry === 'backup.mjs' || entry === 'sync-research.mjs') assert.equal(record.code, 'ELOOP');
    else assert.match(record.message, /ELOOP/);
  });
}
