#!/usr/bin/env bash
set -euo pipefail
umask 022
if [[ "$EUID" != 0 || "$#" != 4 ]]; then
  echo 'Usage (root): install.sh SOURCE_SHA ARCHIVE_PATH ARCHIVE_SHA256 TRUSTED_NODE_PATH' >&2
  exit 2
fi
source_sha="$1"; archive="$2"; expected_hash="$3"; node_source="$4"
[[ "$source_sha" =~ ^[a-f0-9]{40}$ && "$expected_hash" =~ ^[a-f0-9]{64}$ ]]
[[ -f "$archive" && -x "$node_source" && -x /usr/bin/flock ]]
if [[ -e /opt/tiger-pnl/current && ! -L /opt/tiger-pnl/current ]]; then
  echo 'Refusing an unmanaged current directory or file.' >&2; exit 2
fi
exec 9>/run/lock/tiger-pnl-deploy.lock
/usr/bin/flock --nonblock 9 || { echo 'Another Tiger deployment is running.' >&2; exit 1; }
echo "$expected_hash  $archive" | sha256sum --check --status
"$node_source" -e 'if (Number(process.versions.node.split(".")[0]) !== 24) process.exit(1)'
if ! id tiger-pnl >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/tiger-pnl --shell /usr/sbin/nologin tiger-pnl
fi
install -d -m 0755 /opt/tiger-pnl /opt/tiger-pnl/releases /opt/tiger-pnl/runtime
install -d -m 0700 /etc/tiger-pnl
install -d -o tiger-pnl -g tiger-pnl -m 0700 /var/lib/tiger-pnl /var/backups/tiger-pnl
install -d -m 0755 /var/lib/tiger-pnl-acme
if [[ ! -f /etc/tiger-pnl/runtime.env ]]; then
  echo 'Place the protected runtime.env in /etc/tiger-pnl/runtime.env before first installation.' >&2
  exit 2
fi
chown root:root /etc/tiger-pnl/runtime.env
chmod 0600 /etc/tiger-pnl/runtime.env
release="/opt/tiger-pnl/releases/$source_sha"
if [[ -e "$release" ]]; then
  echo 'Release already exists; verify its receipt before reusing it.' >&2
  exit 2
fi
# Validate paths before extracting the verified package. Only the independent website is deployed.
python3 - "$archive" <<'PY'
import pathlib,sys,tarfile
with tarfile.open(sys.argv[1],'r:gz') as package:
    members=package.getmembers()
    if not members: raise ValueError('Empty website package')
    for member in members:
        path=pathlib.PurePosixPath(member.name)
        if not (path.parts and path.parts[0]=='pnl' and '..' not in path.parts and not path.is_absolute()): raise ValueError('Invalid archive path')
        if not (member.isdir() or member.isfile()): raise ValueError('Website package must contain only files and directories')
PY
install -d -m 0755 "$release"
tar --no-same-owner -xzf "$archive" -C "$release"
find "$release" -type d -exec chmod 0755 {} +
find "$release" -type f -exec chmod 0644 {} +
chmod 0755 "$release"/pnl/deploy/*.sh
"$node_source" --check "$release/pnl/server.mjs"
runuser -u tiger-pnl -- test -r "$release/pnl/public/index.html"
previous="$(readlink -f /opt/tiger-pnl/current || true)"
if [[ -n "$previous" && -f "$previous/pnl/backup.mjs" ]]; then
  runuser -u tiger-pnl -- /opt/tiger-pnl/runtime/node "$previous/pnl/backup.mjs" \
    --data-dir /var/lib/tiger-pnl --destination "/var/backups/tiger-pnl/pre-update-$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
# Snapshot every mutable application component before beginning the switch.
rollback_dir="$(mktemp -d /opt/tiger-pnl/.rollback.XXXXXX)"
units=(tiger-pnl.service tiger-pnl-backup.service tiger-pnl-backup.timer)
for unit in "${units[@]}"; do
  if [[ -e "/etc/systemd/system/$unit" || -L "/etc/systemd/system/$unit" ]]; then
    cp -a "/etc/systemd/system/$unit" "$rollback_dir/$unit"
  fi
done
if [[ -e /opt/tiger-pnl/runtime/node ]]; then cp -a /opt/tiger-pnl/runtime/node "$rollback_dir/node"; fi
if [[ -L /opt/tiger-pnl/current ]]; then cp -a /opt/tiger-pnl/current "$rollback_dir/current"; fi
was_active=false; was_timer_active=false; was_enabled=false; was_timer_enabled=false
systemctl is-active --quiet tiger-pnl.service && was_active=true
systemctl is-active --quiet tiger-pnl-backup.timer && was_timer_active=true
systemctl is-enabled --quiet tiger-pnl.service 2>/dev/null && was_enabled=true
systemctl is-enabled --quiet tiger-pnl-backup.timer 2>/dev/null && was_timer_enabled=true
health_check() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if curl --silent --fail --max-time 3 http://127.0.0.1:4348/healthz | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status")=="ok" else 1)' 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}
rollback() {
  local failure="$?" unit
  trap - ERR
  set +e
  systemctl stop tiger-pnl.service tiger-pnl-backup.timer
  for unit in "${units[@]}"; do
    rm -f "/etc/systemd/system/$unit"
    if [[ -e "$rollback_dir/$unit" || -L "$rollback_dir/$unit" ]]; then cp -a "$rollback_dir/$unit" "/etc/systemd/system/$unit"; fi
  done
  rm -f /opt/tiger-pnl/current /opt/tiger-pnl/runtime/node "/opt/tiger-pnl/current.next.$$" "/opt/tiger-pnl/runtime/node.next.$$"
  if [[ -L "$rollback_dir/current" ]]; then cp -a "$rollback_dir/current" /opt/tiger-pnl/current; fi
  if [[ -f "$rollback_dir/node" ]]; then cp -a "$rollback_dir/node" /opt/tiger-pnl/runtime/node; fi
  systemctl daemon-reload
  if [[ "$was_enabled" == true ]]; then systemctl enable tiger-pnl.service; else systemctl disable tiger-pnl.service 2>/dev/null; fi
  if [[ "$was_timer_enabled" == true ]]; then systemctl enable tiger-pnl-backup.timer; else systemctl disable tiger-pnl-backup.timer 2>/dev/null; fi
  if [[ "$was_active" == true ]]; then
    if systemctl start tiger-pnl.service && health_check; then echo 'Previous Tiger service restored and healthy.' >&2;
    else echo 'CRITICAL: previous Tiger service could not be restored; inspect journalctl.' >&2; fi
  fi
  if [[ "$was_timer_active" == true ]]; then systemctl start tiger-pnl-backup.timer; fi
  echo "Candidate installation failed; application files restored. Snapshot: $rollback_dir" >&2
  exit "$failure"
}
trap rollback ERR
install -m 0755 "$node_source" "/opt/tiger-pnl/runtime/node.next.$$"
cmp -s "$node_source" "/opt/tiger-pnl/runtime/node.next.$$"
mv -f "/opt/tiger-pnl/runtime/node.next.$$" /opt/tiger-pnl/runtime/node
install -m 0644 "$release/pnl/deploy/tiger-pnl.service" /etc/systemd/system/tiger-pnl.service
install -m 0644 "$release/pnl/deploy/tiger-pnl-backup.service" /etc/systemd/system/tiger-pnl-backup.service
install -m 0644 "$release/pnl/deploy/tiger-pnl-backup.timer" /etc/systemd/system/tiger-pnl-backup.timer
ln -s "$release" "/opt/tiger-pnl/current.next.$$"
mv -Tf "/opt/tiger-pnl/current.next.$$" /opt/tiger-pnl/current
systemctl daemon-reload
systemctl enable tiger-pnl.service
systemctl restart tiger-pnl.service
health_check
systemctl enable --now tiger-pnl-backup.timer
printf '{"sourceSha":"%s","archiveSha256":"%s","service":"tiger-pnl","port":4348}\n' "$source_sha" "$expected_hash" \
  > "$release/deployment.json"
trap - ERR
echo 'Tiger PnL application installed on loopback; public activation is a separate verified step.'
