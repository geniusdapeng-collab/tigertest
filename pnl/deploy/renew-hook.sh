#!/usr/bin/env bash
set -euo pipefail
if [[ "${RENEWED_LINEAGE:-}" == /etc/letsencrypt/live/tiger-pnl-tigerpnl.com ]]; then
  /usr/sbin/nginx -t
  /usr/bin/systemctl reload nginx.service
fi
