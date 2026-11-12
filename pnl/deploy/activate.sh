#!/usr/bin/env bash
set -euo pipefail
[[ "$EUID" == 0 ]]
exec 9>/run/lock/tiger-pnl-deploy.lock
/usr/bin/flock --nonblock 9 || { echo 'Another Tiger deployment is running.' >&2; exit 1; }
release="$(readlink -f /opt/tiger-pnl/current)"
curl --silent --fail --max-time 5 http://127.0.0.1:4348/healthz | python3 -c \
  'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("records",0)>=1 and d.get("research",0)>=1 else "First genuine account and verified research records are required")'
test -f /etc/letsencrypt/live/tiger-pnl-tigerpnl.com/fullchain.pem
nginx -t
backup="$(mktemp -d /etc/nginx/.tiger-pnl-rollback.XXXXXX)"
paths=(/etc/nginx/sites-available/tiger-pnl.conf /etc/nginx/sites-enabled/tiger-pnl.conf /etc/letsencrypt/renewal-hooks/deploy/tiger-pnl-nginx.sh)
for index in "${!paths[@]}"; do
  if [[ -e "${paths[$index]}" || -L "${paths[$index]}" ]]; then cp -a "${paths[$index]}" "$backup/$index"; fi
done
rollback() {
  local failure="$?" index
  trap - ERR
  set +e
  for index in "${!paths[@]}"; do
    rm -f "${paths[$index]}"
    if [[ -e "$backup/$index" || -L "$backup/$index" ]]; then cp -a "$backup/$index" "${paths[$index]}"; fi
  done
  if nginx -t && systemctl reload nginx.service; then echo 'Previous Nginx configuration restored.' >&2;
  else echo 'CRITICAL: restored Nginx configuration requires operator inspection.' >&2; fi
  exit "$failure"
}
trap rollback ERR
install -m 0644 "$release/pnl/deploy/nginx-https.conf" /etc/nginx/sites-available/tiger-pnl.conf
ln -sfn /etc/nginx/sites-available/tiger-pnl.conf /etc/nginx/sites-enabled/tiger-pnl.conf
nginx -t
install -m 0755 "$release/pnl/deploy/renew-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/tiger-pnl-nginx.sh
systemctl reload nginx.service
trap - ERR
echo 'Tiger PnL exact-domain HTTPS virtual host activated.'
