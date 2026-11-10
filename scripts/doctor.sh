#!/usr/bin/env bash
# Tiger setup diagnostics delegate to the same fixed runtime used by requests.
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON_BIN="${TIGER_PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ] && [ -x ".venv/bin/python" ]; then
  PYTHON_BIN="$PWD/.venv/bin/python"
fi
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

NODE_BIN="${TIGER_NODE_BIN:-$(command -v node || true)}"
if [ -z "$NODE_BIN" ] && [ -x "/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node" ]; then
  NODE_BIN="/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
fi

if [ -z "$PYTHON_BIN" ] || [ -z "$NODE_BIN" ]; then
  echo "missing Python or Node runtime" >&2
  exit 1
fi

exec "$NODE_BIN" scripts/tiger-agent.mjs doctor --python "$PYTHON_BIN" --mode readonly
