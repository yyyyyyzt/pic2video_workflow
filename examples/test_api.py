"""API 调用测试脚本：提交任务 → 轮询进度 → 下载结果。

用法：
    python examples/test_api.py --image face.jpg --video performance.mp4 \
        --prompt "一位金发男士穿黑色西装在街头演奏小提琴" \
        --server http://127.0.0.1:8000 --output final.mp4
"""

from __future__ import annotations

import argparse
import sys
import time

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="ScailSwap API 测试客户端")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--image", required=True, help="源角色照片")
    parser.add_argument("--video", required=True, help="参考视频（1~2 分钟）")
    parser.add_argument("--prompt", default="", help="描述替换后的画面")
    parser.add_argument("--mode", default="replacement", choices=["replacement", "animation"])
    parser.add_argument("--output", default="result.mp4")
    parser.add_argument("--params-json", default="{}", help='额外参数，如 {"seed": 42}')
    args = parser.parse_args()

    base = args.server.rstrip("/")

    # 1) 健康检查
    health = requests.get(f"{base}/health", timeout=30).json()
    print(f"服务状态：{health}")
    if not health.get("ok"):
        print("⚠️ 引擎不可用（ComfyUI 未启动？），任务可能失败", file=sys.stderr)

    # 2) 提交任务
    with open(args.image, "rb") as img, open(args.video, "rb") as vid:
        resp = requests.post(
            f"{base}/api/v1/jobs",
            files={"source_image": img, "target_video": vid},
            data={"prompt": args.prompt, "mode": args.mode, "params_json": args.params_json},
            timeout=600,
        )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    print(f"任务已提交：{job_id}")

    # 3) 轮询进度
    while True:
        status = requests.get(f"{base}/api/v1/jobs/{job_id}", timeout=30).json()
        print(f"\r[{status['percent']:5.1f}%] {status['stage']:<12} {status['message']:<60}", end="", flush=True)
        if status["status"] == "done":
            print()
            break
        if status["status"] == "failed":
            print(f"\n任务失败：{status['error']}", file=sys.stderr)
            return 1
        time.sleep(3)

    # 4) 下载结果
    with requests.get(f"{base}/api/v1/jobs/{job_id}/download", stream=True, timeout=600) as dl:
        dl.raise_for_status()
        with open(args.output, "wb") as fh:
            for block in dl.iter_content(1 << 20):
                fh.write(block)
    print(f"已下载：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
