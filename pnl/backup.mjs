import { constants } from 'node:fs';
import { mkdir, open, readdir, realpath, stat } from 'node:fs/promises';
import { basename, dirname, isAbsolute, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isCliEntry } from './lib/cli.mjs';
import { Ledger, ResearchLedger } from './lib/ledger.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const RECORD = /^\d{12}\.json$/;
const belongsTo = (path, parent) => path === parent || path.startsWith(`${parent}${sep}`);
async function syncDir(path) { const fd = await open(path, 'r'); try { await fd.sync(); } finally { await fd.close(); } }
async function futurePath(path) {
  let parent = dirname(path); const parts = [basename(path)];
  for (;;) {
    try { return resolve(await realpath(parent), ...parts); }
    catch (cause) { if (cause.code !== 'ENOENT') throw cause; parts.unshift(basename(parent)); parent = dirname(parent); }
  }
}
async function filesAt(directory, optional = false) {
  let entries;
  try { entries = await readdir(directory); } catch (cause) { if (optional && cause.code === 'ENOENT') return []; throw cause; }
  const records = entries.filter(name => RECORD.test(name)).sort();
  if (entries.some(name => !RECORD.test(name) && !/^\.pending-[a-f0-9-]+$/.test(name))) throw new Error('Unexpected source record file; backup refused');
  for (let i = 0; i < records.length; i++) if (records[i] !== `${String(i + 1).padStart(12, '0')}.json`) throw new Error('Source record prefix is not contiguous');
  return records;
}
async function copyPrefix(source, destination, filenames) {
  await mkdir(destination, { recursive: true, mode: 0o700 });
  for (const name of filenames) {
    const input = await open(resolve(source, name), constants.O_RDONLY | constants.O_NOFOLLOW); let output;
    try {
      const info = await input.stat();
      if (!info.isFile() || info.size > 1_100_000) throw new Error('Source record must be a bounded regular file');
      const bytes = await input.readFile();
      if (bytes.length !== info.size) throw new Error('Source record changed during backup');
      output = await open(resolve(destination, name), 'wx', 0o600); await output.writeFile(bytes); await output.sync();
    } finally { if (output) await output.close(); await input.close(); }
  }
  await syncDir(destination);
}

/** Source is read-only; the restore verification runs only in a new isolated destination. */
export async function backupLedger({ dataDir, destination }) {
  if (!isAbsolute(dataDir ?? '') || !isAbsolute(destination ?? '')) throw new Error('Backup source and destination must be absolute paths');
  const source = await realpath(dataDir); const sourceInfo = await stat(source);
  if (!sourceInfo.isDirectory()) throw new Error('Backup source must be a directory');
  const target = await futurePath(resolve(destination));
  const sourceRoot = await realpath(ROOT);
  if (belongsTo(target, source) || belongsTo(source, target) || belongsTo(target, sourceRoot)) throw new Error('Backup destination must be isolated from source and repository');
  // Snapshot both independent immutable prefixes before copying. Concurrent appends may continue.
  const accountFiles = await filesAt(resolve(source, 'records'));
  const researchFiles = await filesAt(resolve(source, 'research', 'records'), true);
  await mkdir(dirname(target), { recursive: true, mode: 0o700 });
  await mkdir(target, { mode: 0o700 }); // Exclusive: existing destinations are never merged or overwritten.
  await copyPrefix(resolve(source, 'records'), resolve(target, 'records'), accountFiles);
  await copyPrefix(resolve(source, 'research', 'records'), resolve(target, 'research', 'records'), researchFiles);
  let account; let research; let summary;
  try {
    account = await Ledger.open(target); research = await ResearchLedger.open(resolve(target, 'research'));
    if (account.entries.length !== accountFiles.length || research.entries.length !== researchFiles.length) throw new Error('Restored chain length differs from the captured source prefix');
    summary = { schemaVersion: 1, status: 'backup_restored_and_verified', createdAt: new Date().toISOString(), account: { files: accountFiles.length, headHash: account.entries.at(-1)?.hash ?? null }, research: { files: researchFiles.length, headHash: research.entries.at(-1)?.hash ?? null } };
  } finally {
    const closed = await Promise.allSettled([research?.close(), account?.close()]);
    await syncDir(resolve(target, 'research')); await syncDir(target); await syncDir(dirname(target));
    for (const result of closed) if (result.status === 'rejected') throw result.reason;
  }
  const output = await open(resolve(target, 'backup-summary.json'), 'wx', 0o600);
  try { await output.writeFile(`${JSON.stringify(summary)}\n`); await output.sync(); } finally { await output.close(); }
  await syncDir(target);
  return summary;
}
export function parseArgs(args) {
  const options = {}; const allowed = new Map([['--data-dir', 'dataDir'], ['--destination', 'destination']]);
  for (let i = 0; i < args.length; i++) { const name = allowed.get(args[i]); if (!name || Object.hasOwn(options, name) || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: node pnl/backup.mjs --data-dir /private/data --destination /private/new-backup'); options[name] = args[++i]; }
  if (!options.dataDir || !options.destination) throw new Error('Both --data-dir and --destination are required');
  return options;
}
try { if (await isCliEntry(import.meta.url)) console.log(JSON.stringify(await backupLedger(parseArgs(process.argv.slice(2))))); }
catch (cause) { console.error(JSON.stringify({ status: 'failed', code: cause.code ?? 'BACKUP_VERIFICATION_FAILED', message: 'Backup or isolated restore verification failed; inspect the protected destination and retry with a new destination' })); process.exitCode = 1; }
