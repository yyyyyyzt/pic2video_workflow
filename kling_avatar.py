#!/usr/bin/env python3
"""可灵 AI 数字人（数字角色 2.0）视频生成 —— 最小化单文件实现。

一张角色图 + 一段音频（2~60 秒）+ 一句提示词  →  数字人口播视频。

用法示例：
    python kling_avatar.py --image face.jpg --audio speech.mp3 \
        --prompt "耐心、温柔地讲解，保持微笑，偶尔用手势辅助说明，动作自然" \
        --mode std --output result.mp4

API 文档: https://klingai.com/document-api/api/video/avatar
接口:     POST /v1/videos/avatar/image2video
          GET  /v1/videos/avatar/image2video/{task_id}
鉴权:     AccessKey + SecretKey 生成 JWT（HS256, 30 分钟有效），Authorization: Bearer <token>
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
import time
from pathlib import Path

import jwt  # PyJWT
import requests

DEFAULT_BASE_URL = "https://api-beijing.klingai.com"  # 海外服务器改用 https://api-singapore.klingai.com
CREATE_PATH = "/v1/videos/avatar/image2video"

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac"}
IMAGE_MAX_MB = 10
AUDIO_MAX_MB = 5


def load_dotenv(path: str = ".env") -> None:
    """极简 .env 加载：KEY=VALUE 按行读取，不覆盖已有环境变量。"""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def make_token(access_key: str, secret_key: str) -> str:
    """按可灵官方规范生成 JWT：iss=AK, exp=now+30min, nbf=now-5s。"""
    now = int(time.time())
    payload = {"iss": access_key, "exp": now + 1800, "nbf": now - 5}
    return jwt.encode(payload, secret_key, algorithm="HS256", headers={"alg": "HS256", "typ": "JWT"})


def encode_media(source: str, kind: str) -> str:
    """本地文件 → 无前缀 Base64；URL 原样透传（可灵两种都接受）。"""
    if source.startswith(("http://", "https://")):
        return source

    path = Path(source)
    if not path.is_file():
        sys.exit(f"错误：找不到{kind}文件 {source}")

    if kind == "图片":
        allowed, max_mb = IMAGE_EXTS, IMAGE_MAX_MB
    else:
        allowed, max_mb = AUDIO_EXTS, AUDIO_MAX_MB
    if path.suffix.lower() not in allowed:
        sys.exit(f"错误：{kind}仅支持 {'/'.join(sorted(allowed))} 格式，收到 {path.suffix}")
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > max_mb:
        sys.exit(f"错误：{kind}文件 {size_mb:.1f}MB 超过 {max_mb}MB 上限")

    # 注意：可灵要求纯 Base64 字符串，不能带 data:xxx;base64, 前缀
    return base64.b64encode(path.read_bytes()).decode("ascii")


def create_task(base_url: str, headers: dict, body: dict) -> str:
    resp = requests.post(base_url + CREATE_PATH, headers=headers, json=body, timeout=60)
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 0:
        sys.exit(f"创建任务失败 (HTTP {resp.status_code}): code={data.get('code')} message={data.get('message')}")
    task_id = data["data"]["task_id"]
    print(f"任务已提交，task_id = {task_id}")
    return task_id


def poll_task(base_url: str, token_factory, task_id: str, interval: int, timeout: int) -> str:
    """轮询直到 succeed，返回视频 URL。"""
    deadline = time.time() + timeout
    url = f"{base_url}{CREATE_PATH}/{task_id}"
    while time.time() < deadline:
        headers = {"Authorization": f"Bearer {token_factory()}"}
        resp = requests.get(url, headers=headers, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            sys.exit(f"查询任务失败: code={data.get('code')} message={data.get('message')}")

        task = data["data"]
        status = task.get("task_status")
        elapsed = int(time.time() - (deadline - timeout))
        print(f"[{elapsed:>4d}s] 状态: {status}")

        if status == "succeed":
            videos = task["task_result"]["videos"]
            video = videos[0]
            print(f"生成成功！时长 {video.get('duration', '?')} 秒")
            return video["url"]
        if status == "failed":
            sys.exit(f"任务失败: {task.get('task_status_msg', '(无失败原因)')}")

        time.sleep(interval)
    sys.exit(f"超时：{timeout} 秒内任务未完成，可稍后用 task_id={task_id} 手动查询")


def download(url: str, output: str) -> None:
    print(f"下载视频: {url}")
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(output, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    size_mb = Path(output).stat().st_size / 1024 / 1024
    print(f"已保存到 {output}（{size_mb:.1f}MB）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="可灵数字人视频生成：角色图 + 音频(2~60s) + 提示词 → 视频",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--image", required=True, help="数字人角色图：本地文件(jpg/jpeg/png, ≤10MB, 宽高≥300px, 宽高比 1:2.5~2.5:1) 或 URL")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audio", help="驱动音频：本地文件(mp3/wav/m4a/aac, ≤5MB, 时长 2~60 秒) 或 URL")
    group.add_argument("--audio-id", help="可灵 TTS 接口生成的音频 ID（与 --audio 二选一）")
    parser.add_argument("--prompt", default="", help="提示词：描述动作、情绪、镜头等，≤2500 字符")
    parser.add_argument("--mode", choices=["std", "pro"], default="std", help="std=标准(性价比) / pro=专家(质量更高)")
    parser.add_argument("--output", default="avatar_output.mp4", help="输出视频路径")
    parser.add_argument("--poll-interval", type=int, default=15, help="轮询间隔（秒）")
    parser.add_argument("--timeout", type=int, default=3600, help="等待超时（秒）；60 秒素材通常需要十几分钟")
    args = parser.parse_args()

    load_dotenv()
    access_key = os.environ.get("KLING_ACCESS_KEY", "")
    secret_key = os.environ.get("KLING_SECRET_KEY", "")
    if not access_key or not secret_key:
        sys.exit("错误：请在 .env 或环境变量中配置 KLING_ACCESS_KEY 和 KLING_SECRET_KEY（见 .env.example）")
    base_url = os.environ.get("KLING_API_BASE", DEFAULT_BASE_URL).rstrip("/")

    body: dict = {"image": encode_media(args.image, "图片"), "mode": args.mode}
    if args.audio:
        body["sound_file"] = encode_media(args.audio, "音频")
    else:
        body["audio_id"] = args.audio_id
    if args.prompt:
        body["prompt"] = args.prompt

    token_factory = lambda: make_token(access_key, secret_key)  # noqa: E731
    headers = {"Authorization": f"Bearer {token_factory()}", "Content-Type": "application/json"}

    task_id = create_task(base_url, headers, body)
    video_url = poll_task(base_url, token_factory, task_id, args.poll_interval, args.timeout)
    download(video_url, args.output)


if __name__ == "__main__":
    main()
