#!/usr/bin/env bash
# 一键启动 ScailSwap API 服务（FastAPI + uvicorn）
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST="${SCAILSWAP_HOST:-0.0.0.0}"
PORT="${SCAILSWAP_PORT:-8000}"

if command -v uv >/dev/null 2>&1 && [ -f "uv.lock" ]; then
  exec uv run uvicorn server.app:app --host "$HOST" --port "$PORT"
else
  exec python3 -m uvicorn server.app:app --host "$HOST" --port "$PORT"
fi
