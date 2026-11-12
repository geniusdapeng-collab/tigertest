#!/usr/bin/env bash
set -euo pipefail
umask 077
destination="/var/backups/tiger-pnl/$(date -u +%Y%m%dT%H%M%SZ)-$$"
exec /opt/tiger-pnl/runtime/node /opt/tiger-pnl/current/pnl/backup.mjs \
  --data-dir /var/lib/tiger-pnl --destination "$destination"
