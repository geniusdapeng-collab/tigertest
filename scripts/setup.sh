#!/usr/bin/env bash
# 老虎交易 · 一键安装（交易内核路径）
# 用法：bash scripts/setup.sh [backend|full]
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="${1:-full}"
if [ "$PROFILE" != "backend" ] && [ "$PROFILE" != "full" ]; then
  echo "profile must be backend or full" >&2
  exit 2
fi
TIGER_PYTHON_BIN="${TIGER_PYTHON_BIN:-}"
if [ -z "$TIGER_PYTHON_BIN" ]; then
  for candidate in python3.12 python3.13 python3.11 python3; do
    resolved="$(command -v "$candidate" || true)"
    if [ -n "$resolved" ] && "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      TIGER_PYTHON_BIN="$resolved"
      break
    fi
  done
fi
if [ -z "$TIGER_PYTHON_BIN" ]; then
  echo "Python 3.11 or newer is required; set TIGER_PYTHON_BIN" >&2
  exit 1
fi
TIGER_NODE_BIN="${TIGER_NODE_BIN:-}"
TIGER_PNPM_BIN="${TIGER_PNPM_BIN:-$(command -v pnpm || true)}"
if [ -z "$TIGER_NODE_BIN" ] && [ "$PROFILE" = "full" ]; then
  TIGER_NODE_BIN="$(command -v node || true)"
fi
for candidate in "$PWD/.tiger-runtime/node/bin/node" "$PWD/.runtime/node/bin/node"; do
  if [ -z "$TIGER_NODE_BIN" ] && [ "$PROFILE" = "full" ] && [ -x "$candidate" ]; then
    TIGER_NODE_BIN="$candidate"
  fi
done

echo "==> [1/4] runtime preflight（$PROFILE）"
PREFLIGHT=(python3 scripts/runtime_preflight.py --profile "$PROFILE" --python-bin "$TIGER_PYTHON_BIN")
if [ "$PROFILE" = "full" ]; then
  if [ -z "$TIGER_NODE_BIN" ]; then
    echo "missing Node 24+ runtime for full profile" >&2
    exit 1
  fi
  PREFLIGHT+=(--node-bin "$TIGER_NODE_BIN")
  if [ -z "$TIGER_PNPM_BIN" ]; then
    echo "missing pnpm 10.14.0 for full profile; set TIGER_PNPM_BIN" >&2
    exit 1
  fi
  PREFLIGHT+=(--pnpm-bin "$TIGER_PNPM_BIN")
fi
"${PREFLIGHT[@]}" --out baseline.json >/dev/null

echo "==> [2/4] 安装依赖"
if [ ! -x .venv/bin/python ]; then
  if ! "$TIGER_PYTHON_BIN" -m venv .venv 2>/dev/null; then
    echo "Python 3.11+ venv support is required" >&2
    exit 1
  fi
  echo "    已创建虚拟环境 .venv"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --require-hashes -r requirements.lock
echo "    依赖安装完成"
if [ "$PROFILE" = "full" ]; then
  if [ -z "$TIGER_PNPM_BIN" ] || [ ! -x "$TIGER_PNPM_BIN" ]; then
    echo "missing pnpm for governance Node dependencies; set TIGER_PNPM_BIN" >&2
    exit 1
  fi
  "$TIGER_PNPM_BIN" --dir governance install --frozen-lockfile --ignore-scripts
  echo "    governance 依赖安装完成"
fi

echo "==> [3/4] 冒烟测试（离线 demo，确定性夹具）"
python -m pytest tests/ -q --ignore=tests/test_provider_official.py -x -k "not smoke" 2>&1 | tail -1

echo "==> [4/4] 自检（网络/数据源/LLM）"
if [ "$PROFILE" = "full" ]; then
  TIGER_PYTHON_BIN="$PWD/.venv/bin/python" TIGER_NODE_BIN="$TIGER_NODE_BIN" \
    bash scripts/doctor.sh >/dev/null
fi
PREFLIGHT_FINAL=(python3 scripts/runtime_preflight.py --profile "$PROFILE"
  --python-bin "$PWD/.venv/bin/python")
if [ "$PROFILE" = "full" ]; then
  PREFLIGHT_FINAL+=(--node-bin "$TIGER_NODE_BIN")
  PREFLIGHT_FINAL+=(--pnpm-bin "$TIGER_PNPM_BIN")
fi
"${PREFLIGHT_FINAL[@]}" --dependencies --out baseline.json >/dev/null

cat <<'TIP'

安装完成。下一步：
  离线演示：  python3 main.py --demo
  真实数据：  python3 main.py --mode daily --universe extended --top 30 --picks 8 --html
  点亮 LLM：  见 docs/QUICKSTART.md「LLM 配置」（叙事/辩论/语义清洗需要）
  官网发布：  bash scripts/publish_site.sh（日报 → site/index.html）
TIP
