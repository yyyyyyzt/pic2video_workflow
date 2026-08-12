#!/usr/bin/env python3
"""可灵 API 共享核心：鉴权、媒体编码、数字人任务、TTS 语音合成。

与 CLI 层的区别：这里出错一律 raise KlingError，不调用 sys.exit，
这样 CLI 和 Web 服务都能复用（Web 里 sys.exit 会把服务进程打掉）。

接口：
    POST /v1/videos/avatar/image2video       创建数字人任务
    GET  /v1/videos/avatar/image2video/{id}  查询任务
    POST /v1/audio/tts                       文本转语音（同步返回 audio_id）
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Callable

import jwt  # PyJWT
import requests

DEFAULT_BASE_URL = "https://api-beijing.klingai.com"  # 海外账号用 https://api-singapore.klingai.com
AVATAR_PATH = "/v1/videos/avatar/image2video"
TTS_PATH = "/v1/audio/tts"

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac"}
IMAGE_MAX_MB = 10
AUDIO_MAX_MB = 5
PROMPT_MAX_CHARS = 2500
TTS_MAX_CHARS = 1000
AUDIO_MIN_SECONDS = 2
AUDIO_MAX_SECONDS = 60

# 可灵内置音色。官方对照表（含试听）：
# https://docs.qingque.cn/s/home/eZQDvafJ4vXQkP8T9ZPvmye8S?identityId=2E1MlYrrPk4
VOICES: list[dict[str, str]] = [
    {"id": "genshin_vindi2", "language": "zh", "label": "沉稳男声（推荐口播）"},
    {"id": "zhinen_xuesheng", "language": "zh", "label": "知性学生"},
    {"id": "tiyuxi_xuedi", "language": "zh", "label": "体育系学弟"},
    {"id": "ai_shatang", "language": "zh", "label": "甜美女声·砂糖"},
    {"id": "genshin_klee2", "language": "zh", "label": "活泼女声·可莉"},
    {"id": "genshin_kirara", "language": "zh", "label": "轻快女声·绮良良"},
    {"id": "ai_kaiya", "language": "zh", "label": "温柔女声·凯亚"},
    {"id": "tiexin_nanyou", "language": "zh", "label": "贴心男友"},
    {"id": "ai_chenjiahao_712", "language": "zh", "label": "成熟男声"},
    {"id": "calm_story1", "language": "zh", "label": "平静叙事"},
    {"id": "oversea_male1", "language": "en", "label": "English · Male"},
    {"id": "uk_man2", "language": "en", "label": "English · UK Male"},
    {"id": "reader_en_m-v1", "language": "en", "label": "English · Reader"},
    {"id": "commercial_lady_en_f-v1", "language": "en", "label": "English · Commercial Lady"},
]


class KlingError(Exception):
    """所有可预期的失败（配置缺失、参数不合法、API 报错）都用它。"""


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
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def make_jwt(access_key: str, secret_key: str) -> str:
    """旧版 AK/SK：按可灵规范生成 JWT（iss=AK, exp=now+30min, nbf=now-5s）。"""
    now = int(time.time())
    payload = {"iss": access_key, "exp": now + 1800, "nbf": now - 5}
    return jwt.encode(payload, secret_key, algorithm="HS256", headers={"alg": "HS256", "typ": "JWT"})


def resolve_auth() -> tuple[str, Callable[[], str]]:
    """返回 (鉴权模式说明, token_factory)。

    优先 KLING_API_KEY（新版单 Key 直接 Bearer）；只有 AK/SK 时，
    两者相同按单 Key 处理，不同则签 JWT。
    """
    api_key = os.environ.get("KLING_API_KEY", "").strip()
    access_key = os.environ.get("KLING_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("KLING_SECRET_KEY", "").strip()

    # 兼容：把同一个 api-key-kling-xxx 填进了 AK 和 SK
    if not api_key and access_key and (not secret_key or secret_key == access_key):
        api_key = access_key
    if not api_key and secret_key.startswith("api-key-") and (not access_key or access_key == secret_key):
        api_key = secret_key

    if api_key:
        return "api-key (Bearer 直传)", (lambda: api_key)
    if access_key and secret_key:
        return "ak/sk (JWT)", (lambda: make_jwt(access_key, secret_key))

    raise KlingError(
        "未配置鉴权信息（见 .env.example）：\n"
        "  新版单 Key：KLING_API_KEY=api-key-kling-...\n"
        "  或旧版双钥：KLING_ACCESS_KEY + KLING_SECRET_KEY"
    )


def base_url() -> str:
    return os.environ.get("KLING_API_BASE", DEFAULT_BASE_URL).rstrip("/")


def encode_media(source: str, kind: str) -> str:
    """本地文件 → 无前缀 Base64；URL 原样透传（可灵两种都接受）。"""
    if source.startswith(("http://", "https://")):
        return source

    path = Path(source)
    if not path.is_file():
        raise KlingError(f"找不到{kind}文件 {source}")

    allowed, max_mb = (IMAGE_EXTS, IMAGE_MAX_MB) if kind == "图片" else (AUDIO_EXTS, AUDIO_MAX_MB)
    if path.suffix.lower() not in allowed:
        raise KlingError(f"{kind}仅支持 {'/'.join(sorted(allowed))} 格式，收到 {path.suffix}")
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > max_mb:
        raise KlingError(f"{kind}文件 {size_mb:.1f}MB 超过 {max_mb}MB 上限")

    # 可灵要求纯 Base64 字符串，不能带 data:xxx;base64, 前缀
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _request(method: str, path: str, token: str, *, json_body: dict | None = None, retries: int = 3) -> dict:
    """带重试的 API 调用。偶发 SSL/网络抖动重试，业务错误直接抛。"""
    url = base_url() + path
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last_network_error: Exception | None = None

    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=120)
        except requests.RequestException as exc:
            last_network_error = exc
            time.sleep(2 ** attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            raise KlingError(f"接口返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}") from None

        if resp.status_code != 200 or data.get("code") != 0:
            message = data.get("message", "")
            hint = ""
            if "access key not found" in str(message):
                hint = ("\n提示：这多半是新版单 Key，请填到 KLING_API_KEY（不要走 JWT）；"
                        "也检查域名是否与账号区域匹配（国内 api-beijing / 海外 api-singapore）。")
            raise KlingError(f"API 报错 (HTTP {resp.status_code}) code={data.get('code')}: {message}{hint}")
        return data

    raise KlingError(f"网络请求失败（已重试 {retries} 次）: {last_network_error}")


def synthesize_speech(token: str, text: str, voice_id: str, language: str = "zh",
                      speed: float = 1.0) -> dict:
    """文本转语音。同步接口，返回 {audio_id, url, duration}。

    audio_id 可直接作为数字人接口的 audio_id 使用。
    """
    text = text.strip()
    if not text:
        raise KlingError("台词为空")
    if len(text) > TTS_MAX_CHARS:
        raise KlingError(f"台词 {len(text)} 字，超过 TTS 单次 {TTS_MAX_CHARS} 字上限，请拆分或精简")
    if not 0.8 <= speed <= 2.0:
        raise KlingError(f"语速 {speed} 超出 0.8~2.0 范围")

    data = _request("POST", TTS_PATH, token, json_body={
        "text": text,
        "voice_id": voice_id,
        "voice_language": language,
        "voice_speed": round(speed, 1),
    })
    audios = data.get("data", {}).get("task_result", {}).get("audios") or []
    if not audios:
        raise KlingError(f"TTS 未返回音频: {data}")

    audio = audios[0]
    return {
        "audio_id": audio["id"],
        "url": audio.get("url", ""),
        "duration": float(audio.get("duration") or 0.0),
    }


def create_avatar_task(token: str, *, image: str, audio: str | None = None,
                       audio_id: str | None = None, prompt: str = "",
                       mode: str = "std") -> str:
    """创建数字人任务，返回 task_id。image/audio 传本地路径或 URL 均可。"""
    if bool(audio) == bool(audio_id):
        raise KlingError("audio 与 audio_id 必须二选一（不能都给，也不能都不给）")
    if prompt and len(prompt) > PROMPT_MAX_CHARS:
        raise KlingError(f"提示词 {len(prompt)} 字符，超过 {PROMPT_MAX_CHARS} 上限")
    if mode not in ("std", "pro"):
        raise KlingError(f"mode 只能是 std 或 pro，收到 {mode!r}")

    body: dict = {"image": encode_media(image, "图片"), "mode": mode}
    if audio:
        body["sound_file"] = encode_media(audio, "音频")
    else:
        body["audio_id"] = audio_id
    if prompt:
        body["prompt"] = prompt

    data = _request("POST", AVATAR_PATH, token, json_body=body)
    return data["data"]["task_id"]


def get_avatar_task(token: str, task_id: str) -> dict:
    """查询任务，返回 {status, message, videos:[{url,duration}]}。"""
    data = _request("GET", f"{AVATAR_PATH}/{task_id}", token)
    task = data.get("data", {})
    videos = (task.get("task_result") or {}).get("videos") or []
    return {
        "status": task.get("task_status", ""),
        "message": task.get("task_status_msg", ""),
        "videos": [{"url": v.get("url", ""), "duration": v.get("duration", "")} for v in videos],
    }


def download(url: str, output: str | Path, chunk: int = 1 << 20) -> Path:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(out, "wb") as f:
            for part in resp.iter_content(chunk_size=chunk):
                f.write(part)
    return out
