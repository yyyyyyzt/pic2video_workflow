#!/usr/bin/env bash
# RoleSwap Web 测试页面启动脚本（uv + gunicorn）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# 确保依赖已同步（幂等，已安装则很快跳过）
uv sync --frozen >/dev/null 2>&1 || uv sync

# 加载 .env（若存在）
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HOST="${ROLESWAP_WEB_HOST:-0.0.0.0}"
PORT="${ROLESWAP_WEB_PORT:-7860}"
WORKERS="${ROLESWAP_WEB_WORKERS:-1}"

echo "启动 RoleSwap Web：http://${HOST}:${PORT} （workers=${WORKERS}）"
echo "健康检查：curl http://127.0.0.1:${PORT}/health"

exec uv run gunicorn \
  --bind "${HOST}:${PORT}" \
  --workers "${WORKERS}" \
  --threads 4 \
  --timeout 3600 \
  --access-logfile - \
  --error-logfile - \
  "web.app:app"
