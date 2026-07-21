#!/usr/bin/env bash
# 使用 uv 同步项目依赖（含开发依赖）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv sync --all-groups
echo "依赖已同步。启动 Web：./scripts/start_web.sh"
