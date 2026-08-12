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

鉴权（二选一）：
  1) 新版单 Key：KLING_API_KEY=api-key-kling-...  → Authorization: Bearer <key>
  2) 旧版 AK/SK：KLING_ACCESS_KEY + KLING_SECRET_KEY → JWT(HS256) → Bearer <jwt>
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

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


def make_jwt(access_key: str, secret_key: str) -> str:
    """旧版 AK/SK：按可灵规范生成 JWT（iss=AK, exp=now+30min, nbf=now-5s）。"""
    now = int(time.time())
    payload = {"iss": access_key, "exp": now + 1800, "nbf": now - 5}
    return jwt.encode(payload, secret_key, algorithm="HS256", headers={"alg": "HS256", "typ": "JWT"})


def resolve_auth() -> tuple[str, Callable[[], str]]:
    """返回 (鉴权模式说明, token_factory)。

    优先使用 KLING_API_KEY（新版单 Key 直接 Bearer）。
    若只有 AK/SK：两者相同时按单 Key 处理；不同时走 JWT。
    """
    api_key = os.environ.get("KLING_API_KEY", "").strip()
    access_key = os.environ.get("KLING_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("KLING_SECRET_KEY", "").strip()

    # 兼容：用户把同一个 api-key-kling-xxx 填进了 AK 和 SK
    if not api_key and access_key and (not secret_key or secret_key == access_key):
        api_key = access_key
    if not api_key and secret_key.startswith("api-key-") and (not access_key or access_key == secret_key):
        api_key = secret_key

    if api_key:
        return "api-key (Bearer 直传)", (lambda: api_key)

    if access_key and secret_key:
        return "ak/sk (JWT)", (lambda: make_jwt(access_key, secret_key))

    sys.exit(
        "错误：请配置鉴权信息（见 .env.example）\n"
        "  新版单 Key：KLING_API_KEY=api-key-kling-...\n"
        "  或旧版双钥：KLING_ACCESS_KEY + KLING_SECRET_KEY"
    )


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
        sys.exit(
            f"创建任务失败 (HTTP {resp.status_code}): code={data.get('code')} message={data.get('message')}\n"
            "提示：若 message=access key not found，你拿到的多半是新版单 Key，"
            "请把密钥填到 KLING_API_KEY（或让 AK==SK），不要用 JWT。"
        )
    task_id = data["data"]["task_id"]
    print(f"任务已提交，task_id = {task_id}")
    return task_id


def poll_task(base_url: str, token_factory: Callable[[], str], task_id: str, interval: int, timeout: int) -> str:
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


def load_prompt(path: str) -> str:
    """从文本文件读取提示词。以 # 开头的整行视为注释；空行保留为段落分隔。"""
    p = Path(path)
    if not p.is_file():
        sys.exit(f"错误：找不到提示词文件 {path}")
    lines = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line.rstrip())
    text = "\n".join(lines).strip()
    # 连续空行压成单空行，避免注释删完后留出大片空白
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if not text:
        sys.exit(f"错误：提示词文件为空（或全是注释）: {path}")
    if len(text) > 2500:
        sys.exit(f"错误：提示词 {len(text)} 字符，超过可灵 2500 字符上限（文件: {path}）")
    return text


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
    parser.add_argument("--prompt-file", help="从文本文件读取提示词（与 --prompt 二选一；方便编辑测试）。"
                                            "文件里以 # 开头的行会被忽略")
    parser.add_argument("--mode", choices=["std", "pro"], default="std", help="std=标准(性价比) / pro=专家(质量更高)")
    parser.add_argument("--output", default="avatar_output.mp4", help="输出视频路径")
    parser.add_argument("--poll-interval", type=int, default=15, help="轮询间隔（秒）")
    parser.add_argument("--timeout", type=int, default=3600, help="等待超时（秒）；60 秒素材通常需要十几分钟")
    parser.add_argument("--stabilize", nargs="?", const="track", default=None,
                        help="生成后跑抖动后处理（需 opencv-python + ffmpeg）。可传 track/vidstab/deflicker/interp"
                             "或逗号串联；不传值等于 track。原始成片会保留为 *_raw.mp4")
    args = parser.parse_args()

    if args.prompt and args.prompt_file:
        sys.exit("错误：--prompt 与 --prompt-file 只能二选一")
    prompt = load_prompt(args.prompt_file) if args.prompt_file else args.prompt

    load_dotenv()
    auth_mode, token_factory = resolve_auth()
    base_url = os.environ.get("KLING_API_BASE", DEFAULT_BASE_URL).rstrip("/")
    print(f"鉴权模式: {auth_mode} | 域名: {base_url}")
    if prompt:
        print(f"提示词: {len(prompt)} 字符")

    body: dict = {"image": encode_media(args.image, "图片"), "mode": args.mode}
    if args.audio:
        body["sound_file"] = encode_media(args.audio, "音频")
    else:
        body["audio_id"] = args.audio_id
    if prompt:
        body["prompt"] = prompt

    headers = {"Authorization": f"Bearer {token_factory()}", "Content-Type": "application/json"}
    task_id = create_task(base_url, headers, body)
    video_url = poll_task(base_url, token_factory, task_id, args.poll_interval, args.timeout)
    download(video_url, args.output)

    if args.stabilize:
        run_stabilize(args.output, args.stabilize)


def run_stabilize(output: str, method: str) -> None:
    """把成片改名为 *_raw.mp4，后处理结果写回原路径（原始文件始终留底）。"""
    out = Path(output)
    raw = out.with_name(f"{out.stem}_raw{out.suffix}")
    out.replace(raw)
    print(f"\n原始成片已保留为 {raw}")
    print(f"运行后处理（method={method}）…")

    cmd = [sys.executable, str(Path(__file__).with_name("stabilize.py")), str(raw), "-o", str(out), "--method", method]
    if subprocess.run(cmd).returncode != 0:
        raw.replace(out)  # 后处理失败就还原，不留下半成品
        sys.exit("后处理失败，已还原原始成片。可单独调试：python3 stabilize.py <视频> --method analyze")


if __name__ == "__main__":
    main()
