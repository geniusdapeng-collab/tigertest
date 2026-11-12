import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, writeFile, readdir, rm, stat, symlink } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { backupLedger, parseArgs } from '../backup.mjs';
import { Ledger, ResearchLedger } from '../lib/ledger.mjs';
import { digest } from '../lib/schema.mjs';
import { modelResearchFixture } from './model-fixture.mjs';

function account() { return { schemaVersion: 1, idempotencyKey: 'BACKUP-TEST-ONLY-ACCOUNT', asOf: '2026-01-02T21:00:00Z', account: { label: 'BACKUP UNIT TEST PRIVATE ACCOUNT', currency: 'USD', nav: 100000, cash: 100000 }, positions: [], trades: [], decisions: [], netExternalFlow: 0, provenance: { kind: 'operator_attested', evidenceSha256: digest('BACKUP UNIT TEST ONLY'), sourceRef: 'private-backup-unit-test', attestedBy: 'private-test-attestor' } }; }
function research() { return modelResearchFixture({ idempotencyKey: 'BACKUP-TEST-ONLY-RESEARCH' }); }

async function setup(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-backup-test-')); const dataDir = join(path, 'source');
  const a = await Ledger.open(dataDir); const r = await ResearchLedger.open(join(dataDir, 'research')); await a.append(account()); await r.append(research());
  t.after(async () => { await a.close(); await r.close(); await rm(path, { recursive: true, force: true }); });
  return { path, dataDir, a, r, destination: join(path, 'backups', 'new-only') };
}

test('live backup copies immutable prefixes, excludes locks/pending, restores both chains, and never modifies source', async t => {
  const f = await setup(t); const lockBefore = await readFile(join(f.dataDir, 'writer.lock'), 'utf8');
  await writeFile(join(f.dataDir, 'records', '.pending-abcdef'), 'uncommitted unit-test data');
  const result = await backupLedger({ dataDir: f.dataDir, destination: f.destination });
  assert.equal(result.status, 'backup_restored_and_verified'); assert.equal(result.account.files, 1); assert.equal(result.research.files, 1);
  assert.equal(result.account.headHash, f.a.entries[0].hash); assert.equal(result.research.headHash, f.r.entries[0].hash);
  assert.equal(await readFile(join(f.dataDir, 'writer.lock'), 'utf8'), lockBefore);
  assert.deepEqual(await readdir(join(f.destination, 'records')), ['000000000001.json']);
  assert.deepEqual(await readdir(join(f.destination, 'research', 'records')), ['000000000001.json']);
  assert.equal((await readdir(f.destination)).includes('writer.lock'), false); assert.equal((await readdir(join(f.destination, 'research'))).includes('writer.lock'), false);
  const summary = JSON.stringify(result); assert.equal(summary.includes('PRIVATE ACCOUNT'), false); assert.equal(summary.includes('attestedBy'), false);
  assert.equal((await stat(join(f.destination, 'records', '000000000001.json'))).mode & 0o777, 0o600);
  const reopened = await Ledger.open(f.destination); assert.equal(reopened.entries[0].hash, result.account.headHash); await reopened.close();
});

test('existing destination and source/repository overlap are refused without overwriting', async t => {
  const f = await setup(t); await mkdir(f.destination, { recursive: true }); await writeFile(join(f.destination, 'sentinel'), 'keep-existing-data');
  await assert.rejects(backupLedger({ dataDir: f.dataDir, destination: f.destination }), { code: 'EEXIST' });
  assert.equal(await readFile(join(f.destination, 'sentinel'), 'utf8'), 'keep-existing-data');
  await assert.rejects(backupLedger({ dataDir: f.dataDir, destination: join(f.dataDir, 'nested-copy') }), /isolated/);
  await assert.rejects(backupLedger({ dataDir: f.dataDir, destination: join(f.dataDir, 'must-not-create-parent', 'nested-copy') }), /isolated/);
  await assert.rejects(stat(join(f.dataDir, 'must-not-create-parent')), { code: 'ENOENT' });
  await symlink(f.dataDir, join(f.path, 'source-link'));
  await assert.rejects(backupLedger({ dataDir: f.dataDir, destination: join(f.path, 'source-link', 'nested-copy') }), /isolated/);
});

test('corrupt accepted chain fails actual isolated restore and produces no success summary', async t => {
  const f = await setup(t); const file = join(f.dataDir, 'records', '000000000001.json');
  const changed = JSON.parse(await readFile(file, 'utf8')); changed.record.account.nav = 200000; changed.record.account.cash = 200000; await writeFile(file, JSON.stringify(changed));
  await assert.rejects(backupLedger({ dataDir: f.dataDir, destination: f.destination }), /integrity/);
  await assert.rejects(stat(join(f.destination, 'backup-summary.json')), { code: 'ENOENT' });
});

test('backup survives concurrent appends by capturing a valid immutable prefix per chain', async t => {
  const f = await setup(t); const original = account();
  const append = Promise.all(Array.from({ length: 5 }, (_, i) => f.a.append({ ...original, idempotencyKey: `backup-concurrent-test-${i}`, asOf: `2026-01-${String(i + 3).padStart(2, '0')}T21:00:00Z` })));
  const result = await backupLedger({ dataDir: f.dataDir, destination: f.destination }); await append;
  assert.ok(result.account.files >= 1 && result.account.files <= 6);
  const restored = await Ledger.open(f.destination); assert.equal(restored.entries.length, result.account.files); assert.equal(restored.entries.at(-1).hash, result.account.headHash); await restored.close();
  assert.equal(f.a.entries.length, 6);
});

test('backup CLI accepts only explicit absolute source and new destination', async () => {
  assert.deepEqual(parseArgs(['--data-dir', '/private/source', '--destination', '/private/new']), { dataDir: '/private/source', destination: '/private/new' });
  assert.throws(() => parseArgs(['--data-dir', '/one', '--data-dir', '/two']), /Usage/);
  await assert.rejects(backupLedger({ dataDir: 'relative', destination: '/private/new' }), /absolute/);
});

for (const kind of ['directory', 'file']) {
  test(`backup CLI through a ${kind} symlink really writes and verifies both chains`, async t => {
    const f = await setup(t); const repository = fileURLToPath(new URL('../../', import.meta.url));
    const current = join(f.path, kind === 'directory' ? 'current' : 'backup-link.mjs');
    await symlink(kind === 'directory' ? repository : join(repository, 'pnl', 'backup.mjs'), current);
    const entry = kind === 'directory' ? join(current, 'pnl', 'backup.mjs') : current;
    const sourceFiles = [join(f.dataDir, 'records', '000000000001.json'), join(f.dataDir, 'research', 'records', '000000000001.json')];
    const before = await Promise.all(sourceFiles.map(file => readFile(file, 'utf8')));
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('TIGER_') && key !== 'NODE_TEST_CONTEXT'));
    const args = [entry, '--data-dir', f.dataDir, '--destination', f.destination];
    const result = spawnSync(process.execPath, args, { cwd: f.path, env, encoding: 'utf8', timeout: 20_000 });
    assert.equal(result.error, undefined); assert.equal(result.signal, null); assert.equal(result.status, 0, result.stderr);
    const summary = JSON.parse(result.stdout);
    assert.equal(summary.status, 'backup_restored_and_verified');
    assert.equal(summary.account.files, 1); assert.equal(summary.research.files, 1);
    assert.equal(summary.account.headHash, f.a.entries[0].hash); assert.equal(summary.research.headHash, f.r.entries[0].hash);
    assert.deepEqual(JSON.parse(await readFile(join(f.destination, 'backup-summary.json'), 'utf8')), summary);
    const restoredAccount = await Ledger.open(f.destination); const restoredResearch = await ResearchLedger.open(join(f.destination, 'research'));
    try {
      assert.equal(restoredAccount.entries[0].hash, summary.account.headHash);
      assert.equal(restoredResearch.entries[0].hash, summary.research.headHash);
    } finally { await Promise.all([restoredResearch.close(), restoredAccount.close()]); }
    assert.deepEqual(await Promise.all(sourceFiles.map(file => readFile(file, 'utf8'))), before);
    const duplicate = spawnSync(process.execPath, args, { cwd: f.path, env, encoding: 'utf8', timeout: 20_000 });
    assert.equal(duplicate.error, undefined); assert.equal(duplicate.status, 1);
    assert.equal(JSON.parse(duplicate.stderr).code, 'EEXIST'); assert.equal(duplicate.stdout, '');
    assert.deepEqual(JSON.parse(await readFile(join(f.destination, 'backup-summary.json'), 'utf8')), summary);
  });
}
