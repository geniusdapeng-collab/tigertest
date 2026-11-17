#!/usr/bin/env bash
# Tiger setup diagnostics delegate to the same fixed runtime used by requests.
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${TIGER_PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ] && [ -x ".venv/bin/python" ]; then
  PYTHON_BIN="$PWD/.venv/bin/python"
fi
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3.12 python3.13 python3.11 python3; do
    resolved="$(command -v "$candidate" || true)"
    if [ -n "$resolved" ] && "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      PYTHON_BIN="$resolved"
      break
    fi
  done
fi

NODE_BIN="${TIGER_NODE_BIN:-$(command -v node || true)}"
PNPM_BIN="${TIGER_PNPM_BIN:-$(command -v pnpm || true)}"
for candidate in "$PWD/.tiger-runtime/node/bin/node" "$PWD/.runtime/node/bin/node"; do
  if [ -z "$NODE_BIN" ] && [ -x "$candidate" ]; then
    NODE_BIN="$candidate"
  fi
done

if [ -z "$PYTHON_BIN" ] || [ -z "$NODE_BIN" ] || [ -z "$PNPM_BIN" ]; then
  echo "missing Python, Node or pnpm runtime" >&2
  exit 1
fi

python3 scripts/runtime_preflight.py --profile full \
  --python-bin "$PYTHON_BIN" --node-bin "$NODE_BIN" --pnpm-bin "$PNPM_BIN" >/dev/null
exec "$NODE_BIN" scripts/tiger-agent.mjs doctor --python "$PYTHON_BIN" --mode readonly
