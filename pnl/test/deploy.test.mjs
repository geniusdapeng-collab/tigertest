import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, symlink, readlink, cp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';

const DEPLOY = resolve(dirname(fileURLToPath(import.meta.url)), '../deploy');
// This harness rewrites every managed host path into a temporary root and replaces
// privilege/service commands. It never contacts Nginx, systemd, DNS or production.
const HELPER = String.raw`
import hashlib,json,os,pathlib,shutil,sys,tarfile
root=pathlib.Path(os.environ['TIGER_DEPLOY_FIXTURE_ROOT'])
operation=sys.argv[1]; args=sys.argv[2:]
statefile=root/'mock-state.json'
state=json.loads(statefile.read_text())
state['commands'].append([operation,*args])
def finish(code=0):
    statefile.write_text(json.dumps(state)); sys.exit(code)
if operation=='systemctl':
    verb=args[0]; names=[x for x in args[1:] if not x.startswith('-')]
    if verb in ('is-active','is-enabled'):
        finish(0 if state['active' if verb=='is-active' else 'enabled'].get(names[-1],False) else 3)
    if verb=='restart':
        state['restartCalls']+=1
        if state['scenario']=='restart-fail' and state['restartCalls']==1: finish(1)
        for name in names: state['active'][name]=True
    if verb in ('start','stop'):
        for name in names: state['active'][name]=verb=='start'
    if verb in ('enable','disable'):
        for name in names:
            state['enabled'][name]=verb=='enable'
            if '--now' in args: state['active'][name]=True
    if verb=='reload':
        state['reloadCalls']+=1
        if state['scenario']=='reload-fail' and state['reloadCalls']==1: finish(1)
elif operation=='nginx':
    state['nginxCalls']+=1
    if state['scenario']=='nginx-test-fail' and state['nginxCalls']==2: finish(1)
elif operation=='curl':
    current=root/'opt/tiger-pnl/current'
    is_candidate=current.is_symlink() and os.readlink(current).endswith('a'*40)
    if state['scenario']=='health-fail' and is_candidate:
        print(json.dumps({'status':'unavailable','records':1,'research':1})); finish()
    count=0 if state['scenario']=='missing-first-record' else 1
    print(json.dumps({'status':'ok','records':count,'research':count}))
elif operation=='install':
    paths=[]; directory=False; mode=0o755; index=0
    while index<len(args):
        arg=args[index]
        if arg=='-d': directory=True
        elif arg in ('-m','-o','-g'):
            index+=1
            if arg=='-m': mode=int(args[index],8)
        else: paths.append(pathlib.Path(arg))
        index+=1
    if directory:
        for path in paths: path.mkdir(parents=True,exist_ok=True); path.chmod(mode)
    else:
        source,dest=paths
        if dest.is_symlink(): dest.unlink()
        shutil.copyfile(source,dest); dest.chmod(mode)
elif operation=='readlink': print(os.path.realpath(args[-1]))
elif operation=='mv':
    paths=[x for x in args if not x.startswith('-')]; os.replace(paths[0],paths[1])
elif operation=='sha256sum':
    expected,path=sys.stdin.read().strip().split('  ',1)
    if hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()!=expected: finish(1)
elif operation=='tar':
    source=args[args.index('-xzf')+1]; target=args[args.index('-C')+1]
    with tarfile.open(source,'r:gz') as archive: archive.extractall(target)
else: raise RuntimeError('Unknown isolated harness operation')
finish()
`;
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
async function exists(path) { try { await stat(path); return true; } catch (cause) { if (cause.code === 'ENOENT') return false; throw cause; } }
async function fixture(t, { scenario, previous = true, oldLink = true }) {
  const root = await mkdtemp(join(tmpdir(), 'tiger-pnl-deploy-fixture-')); t.after(() => rm(root, { recursive: true, force: true }));
  const dirs = ['bin', 'run/lock', 'etc/tiger-pnl', 'etc/systemd/system', 'etc/nginx/sites-available', 'etc/nginx/sites-enabled', 'etc/letsencrypt/live/tiger-pnl-tigerpnl.com', 'etc/letsencrypt/renewal-hooks/deploy', 'opt/tiger-pnl/releases/previous/pnl', 'opt/tiger-pnl/runtime', 'var/lib/tiger-pnl', 'var/backups/tiger-pnl'];
  for (const path of dirs) await mkdir(join(root, path), { recursive: true });
  const helper = join(root, 'mock-command.py'); await writeFile(helper, HELPER);
  const commands = ['install', 'systemctl', 'nginx', 'curl', 'readlink', 'mv', 'sha256sum', 'tar'];
  for (const command of commands) await writeFile(join(root, 'bin', command), `#!/bin/sh\nexec python3 ${quote(helper)} ${command} "$@"\n`, { mode: 0o755 });
  for (const command of ['chown', 'id', 'sleep']) await writeFile(join(root, 'bin', command), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  await writeFile(join(root, 'bin', 'useradd'), '#!/bin/sh\nexit 99\n', { mode: 0o755 });
  await writeFile(join(root, 'bin', 'runuser'), '#!/bin/sh\nshift 3\nexec "$@"\n', { mode: 0o755 });
  await writeFile(join(root, 'bin/flock'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  await writeFile(join(root, 'etc/tiger-pnl/runtime.env'), 'PRIVATE_FIXTURE_CONFIGURATION_ONLY=1\n');
  await writeFile(join(root, 'etc/letsencrypt/live/tiger-pnl-tigerpnl.com/fullchain.pem'), 'FIXTURE CERTIFICATE PLACEHOLDER NEVER SERVED');
  await writeFile(join(root, 'etc/letsencrypt/renewal-hooks/deploy/tiger-pnl-nginx.sh'), 'OLD_HOOK\n');
  const oldRelease = join(root, 'opt/tiger-pnl/releases/previous'); const units = ['tiger-pnl.service', 'tiger-pnl-backup.service', 'tiger-pnl-backup.timer'];
  if (previous) {
    await cp(DEPLOY, join(oldRelease, 'pnl/deploy'), { recursive: true });
    await writeFile(join(oldRelease, 'pnl/server.mjs'), 'OLD_SERVER'); await writeFile(join(oldRelease, 'pnl/backup.mjs'), 'OLD_BACKUP');
    await writeFile(join(root, 'opt/tiger-pnl/runtime/node'), '#!/bin/sh\n# OLD_NODE\nexit 0\n', { mode: 0o755 });
    await symlink(oldRelease, join(root, 'opt/tiger-pnl/current'));
    for (const unit of units) await writeFile(join(root, 'etc/systemd/system', unit), `OLD_${unit}\n`);
  }
  const oldAvailable = join(root, 'etc/nginx/sites-available/tiger-pnl.conf'); const oldEnabled = join(root, 'etc/nginx/sites-enabled/tiger-pnl.conf');
  if (oldLink) { await writeFile(oldAvailable, 'OLD_HTTP_CONF\n'); await writeFile(join(root, 'etc/nginx/sites-available/custom-tiger.conf'), 'OLD_CUSTOM_CONF\n'); await symlink('../sites-available/custom-tiger.conf', oldEnabled); }
  await writeFile(join(root, 'etc/nginx/sites-available/pengmanager.conf'), 'PROTECTED_PENGMANAGER_CONF\n');
  await writeFile(join(root, 'etc/systemd/system/pengmanager.service'), 'PROTECTED_PENGMANAGER_UNIT\n');
  const initial = { scenario, commands: [], restartCalls: 0, reloadCalls: 0, nginxCalls: 0, active: { 'tiger-pnl.service': previous, 'tiger-pnl-backup.timer': previous, 'pengmanager.service': true }, enabled: { 'tiger-pnl.service': previous, 'tiger-pnl-backup.timer': previous, 'pengmanager.service': true } };
  await writeFile(join(root, 'mock-state.json'), JSON.stringify(initial));
  const packageDir = join(root, 'package/pnl'); await mkdir(join(packageDir, 'public'), { recursive: true }); await cp(DEPLOY, join(packageDir, 'deploy'), { recursive: true });
  await writeFile(join(packageDir, 'server.mjs'), 'FIXTURE_ONLY_SERVER'); await writeFile(join(packageDir, 'public/index.html'), 'FIXTURE_ONLY_INDEX');
  const archive = join(root, 'candidate.tar.gz');
  const packed = spawnSync('python3', ['-c', 'import sys,tarfile; a=tarfile.open(sys.argv[2],"w:gz"); a.add(sys.argv[1],arcname="pnl"); a.close()', packageDir, archive], { encoding: 'utf8' }); assert.equal(packed.status, 0, packed.stderr);
  const node = join(root, 'candidate-node'); await writeFile(node, '#!/bin/sh\n# NEW_NODE\nexit 0\n', { mode: 0o755 });
  const digest = createHash('sha256').update(await readFile(archive)).digest('hex');
  async function run(name, extraEnv = {}) {
    let script = await readFile(join(DEPLOY, name), 'utf8');
    script = script.replace('[[ "$EUID" != 0 || "$#" != 4 ]]', '[[ "$#" != 4 ]]').replace('[[ "$EUID" == 0 ]]', 'true');
    const managed = ['/opt/tiger-pnl', '/etc/tiger-pnl', '/etc/systemd/system', '/etc/nginx', '/etc/letsencrypt', '/var/lib/tiger-pnl', '/var/backups/tiger-pnl', '/run/lock'];
    for (const prefix of managed) script = script.replaceAll(prefix, `${root}${prefix}`);
    script = script.replaceAll('/usr/bin/flock', `${root}/bin/flock`);
    const executable = join(root, `isolated-${name}`); await writeFile(executable, script);
    // The isolated Python command harness can exceed 15s on a busy Mac. Keep
    // a bounded process deadline without weakening any rollback assertions.
    return spawnSync('bash', [executable, ...(name === 'install.sh' ? ['a'.repeat(40), archive, digest, node] : [])], { encoding: 'utf8', timeout: 60000, env: { ...process.env, ...extraEnv, PATH: `${join(root, 'bin')}:${process.env.PATH}`, TIGER_DEPLOY_FIXTURE_ROOT: root } });
  }
  async function protectedHostUnchanged() { assert.equal(await readFile(join(root, 'etc/nginx/sites-available/pengmanager.conf'), 'utf8'), 'PROTECTED_PENGMANAGER_CONF\n'); assert.equal(await readFile(join(root, 'etc/systemd/system/pengmanager.service'), 'utf8'), 'PROTECTED_PENGMANAGER_UNIT\n'); const state = JSON.parse(await readFile(join(root, 'mock-state.json'), 'utf8')); assert.equal(state.active['pengmanager.service'], true); assert.equal(state.enabled['pengmanager.service'], true); for (const command of state.commands.filter(c => c[0] === 'systemctl')) assert.equal(command.some(arg => arg.includes('pengmanager')), false); return state; }
  return { root, oldRelease, oldAvailable, oldEnabled, units, run, protectedHostUnchanged };
}

for (const scenario of ['restart-fail', 'health-fail']) test(`isolated install ${scenario} restores application, runtime, units and prior service state`, async t => {
  const f = await fixture(t, { scenario }); const result = await f.run('install.sh'); assert.equal(result.status, 1, result.stderr);
  assert.equal(await readlink(join(f.root, 'opt/tiger-pnl/current')), f.oldRelease); assert.match(await readFile(join(f.root, 'opt/tiger-pnl/runtime/node'), 'utf8'), /OLD_NODE/);
  for (const unit of f.units) assert.equal(await readFile(join(f.root, 'etc/systemd/system', unit), 'utf8'), `OLD_${unit}\n`);
  const state = await f.protectedHostUnchanged(); assert.equal(state.active['tiger-pnl.service'], true); assert.equal(state.enabled['tiger-pnl.service'], true); assert.equal(state.active['tiger-pnl-backup.timer'], true); assert.match(result.stderr, /restored and healthy/);
});
test('isolated first installation restart failure removes new current, runtime and unit files', async t => {
  const f = await fixture(t, { scenario: 'restart-fail', previous: false }); const result = await f.run('install.sh'); assert.equal(result.status, 1, result.stderr);
  assert.equal(await exists(join(f.root, 'opt/tiger-pnl/current')), false); assert.equal(await exists(join(f.root, 'opt/tiger-pnl/runtime/node')), false); for (const unit of f.units) assert.equal(await exists(join(f.root, 'etc/systemd/system', unit)), false);
  const state = await f.protectedHostUnchanged(); assert.equal(state.active['tiger-pnl.service'], false); assert.equal(state.enabled['tiger-pnl.service'], false); assert.equal(state.enabled['tiger-pnl-backup.timer'], false);
});
for (const oldLink of [false, true]) test(`isolated failed Nginx candidate test restores ${oldLink ? 'old custom symlink and configuration' : 'absence of first-install virtual host'}`, async t => {
  const f = await fixture(t, { scenario: 'nginx-test-fail', oldLink }); const result = await f.run('activate.sh'); assert.equal(result.status, 1, result.stderr);
  if (oldLink) { assert.equal(await readFile(f.oldAvailable, 'utf8'), 'OLD_HTTP_CONF\n'); assert.equal(await readlink(f.oldEnabled), '../sites-available/custom-tiger.conf'); }
  else { assert.equal(await exists(f.oldAvailable), false); assert.equal(await exists(f.oldEnabled), false); }
  assert.equal(await readFile(join(f.root, 'etc/letsencrypt/renewal-hooks/deploy/tiger-pnl-nginx.sh'), 'utf8'), 'OLD_HOOK\n'); await f.protectedHostUnchanged(); assert.match(result.stderr, /Previous Nginx configuration restored/);
});
test('isolated failed Nginx reload restores old symlink, configuration and renewal hook then reloads the old configuration', async t => {
  const f = await fixture(t, { scenario: 'reload-fail' }); const result = await f.run('activate.sh'); assert.equal(result.status, 1, result.stderr);
  assert.equal(await readFile(f.oldAvailable, 'utf8'), 'OLD_HTTP_CONF\n'); assert.equal(await readlink(f.oldEnabled), '../sites-available/custom-tiger.conf'); assert.equal(await readFile(join(f.root, 'etc/letsencrypt/renewal-hooks/deploy/tiger-pnl-nginx.sh'), 'utf8'), 'OLD_HOOK\n');
  const state = await f.protectedHostUnchanged(); assert.equal(state.reloadCalls, 2); assert.match(result.stderr, /Previous Nginx configuration restored/);
});
test('Python optimization cannot bypass failed candidate health and rollback', async t => {
  const f = await fixture(t, { scenario: 'health-fail' }); const result = await f.run('install.sh', { PYTHONOPTIMIZE: '1' });
  assert.equal(result.status, 1, result.stderr); assert.equal(await readlink(join(f.root, 'opt/tiger-pnl/current')), f.oldRelease);
  const state = await f.protectedHostUnchanged(); assert.equal(state.active['tiger-pnl.service'], true); assert.match(result.stderr, /restored and healthy/);
});
test('Python optimization cannot bypass first genuine account and research record gate', async t => {
  const f = await fixture(t, { scenario: 'missing-first-record' }); const result = await f.run('activate.sh', { PYTHONOPTIMIZE: '1' });
  assert.notEqual(result.status, 0, result.stdout); assert.equal(await readFile(f.oldAvailable, 'utf8'), 'OLD_HTTP_CONF\n'); assert.equal(await readlink(f.oldEnabled), '../sites-available/custom-tiger.conf');
  const state = await f.protectedHostUnchanged(); assert.equal(state.nginxCalls, 0); assert.equal(state.reloadCalls, 0);
});
