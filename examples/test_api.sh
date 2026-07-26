#!/usr/bin/env bash
# curl 版 API 调用示例
set -euo pipefail

SERVER="${SERVER:-http://127.0.0.1:8000}"
IMAGE="${1:-face.jpg}"
VIDEO="${2:-talk.mp4}"
PROMPT="${3:-一位穿深色衬衫的男士坐在办公室里对着镜头讲话}"
# 生成帧率 / 成片帧率（口播推荐 16 生成、30 输出）
TARGET_FPS="${TARGET_FPS:-16}"
OUTPUT_FPS="${OUTPUT_FPS:-30}"

echo "== 1) 健康检查"
curl -s "$SERVER/health" | python3 -m json.tool

echo "== 2) 提交任务"
JOB_ID=$(curl -s -X POST "$SERVER/api/v1/jobs" \
  -F "source_image=@$IMAGE" \
  -F "target_video=@$VIDEO" \
  -F "prompt=$PROMPT" \
  -F "mode=replacement" \
  -F "target_fps=$TARGET_FPS" \
  -F "output_fps=$OUTPUT_FPS" \
  -F 'params_json={"seed": 42}' | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
echo "job_id=$JOB_ID"

echo "== 3) 轮询进度（Ctrl+C 退出不影响后台生成）"
while true; do
  STATUS=$(curl -s "$SERVER/api/v1/jobs/$JOB_ID")
  echo "$STATUS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"[{d['percent']:5.1f}%] {d['status']} {d['stage']}: {d['message']}\")"
  STATE=$(echo "$STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$STATE" = "done" ] && break
  [ "$STATE" = "failed" ] && { echo "$STATUS" | python3 -m json.tool; exit 1; }
  sleep 3
done

echo "== 4) 下载结果"
curl -s -o result.mp4 "$SERVER/api/v1/jobs/$JOB_ID/download"
echo "已保存 result.mp4"
