#!/usr/bin/env python3
"""腾讯云 TokenHub 视频生成：人像驱动（YT-Video-HumanActor）。

国内通道不再走模力方舟。TokenHub 用 OpenAI 形态的 Bearer Key，提交/查询是
两步异步：

    POST {base}/v1/api/video/submit   提交，返回任务 id
    POST {base}/v1/api/video/query    查询，等到 DONE 再下载

参数按 TokenHub 文档用小写下划线（LogoAdd → logo_add）。底层字段对齐
SubmitHumanActorJob / DescribeHumanActorJob：

    prompt / audio_url / image_url 或 image_base64 / resolution / frame_rate / logo_add

图片可以塞 Base64，音频只要公网 URL。本地音频若配了 WaveSpeed，会借它的
CDN 中转；没有就要求调用方自己给 http(s) 地址。
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Callable

import requests

BASE_URL = "https://tokenhub.tencentmaas.com"
SUBMIT_PATH = "/v1/api/video/submit"
QUERY_PATH = "/v1/api/video/query"

# 1 积分 = 1.2 元。生成失败不计费。
YUAN_PER_CREDIT = 1.2

# 底层人像驱动：WAIT / RUN / FAIL / DONE。
# TokenHub OpenAI 形态还会给 queued / in_progress / completed / failed。
STATUS_OK = {"DONE", "done", "SUCCESS", "success", "completed", "COMPLETED"}
STATUS_BAD = {"FAIL", "fail", "FAILED", "failed", "ERROR", "error"}
STATUS_WAIT = {"WAIT", "wait", "RUN", "run", "RUNNING", "running",
               "PROCESSING", "processing", "QUEUED", "queued", "PENDING", "pending",
               "IN_PROGRESS", "in_progress"}

RECIPES = {
    "yt-video-humanactor": {
        "model": "yt-video-humanactor",
        "resolution": "720p",
        "frame_rate": 25,
        "credits_per_second": 1,
        "label": "优图 HumanActor 720p",
    },
    "yt-video-humanactor-1080": {
        "model": "yt-video-humanactor",
        "resolution": "1080p",
        "frame_rate": 25,
        "credits_per_second": 2,
        "label": "优图 HumanActor 1080p",
    },
}


class TokenHubError(Exception):
    """配置缺失、参数不合法、接口报错、任务失败。"""


def api_key() -> str:
    key = (os.environ.get("TOKENHUB_API_KEY")
           or os.environ.get("TENCENT_TOKENHUB_API_KEY")
           or "").strip()
    if not key:
        raise TokenHubError(
            "未配置 TOKENHUB_API_KEY（见 .env.example）。\n"
            "  在腾讯云 TokenHub 控制台 → API Key 管理创建，形如 sk-xxxx"
        )
    return key


def base_url() -> str:
    return (os.environ.get("TOKENHUB_API_BASE") or BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


def _unwrap(data: dict) -> dict:
    """TokenHub 有时包一层 data / Response，摊平后再读字段。"""
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict):
        return {**data, **inner}
    if isinstance(inner, str) and inner:
        if inner.startswith(("http://", "https://")):
            return {**data, "url": inner}
        return {**data, "id": inner}
    inner = data.get("Response") or data.get("response")
    if isinstance(inner, dict):
        return {**data, **inner}
    return data


def _as_text(value) -> str:
    if isinstance(value, dict):
        inner = value.get("message") or value.get("msg") or value.get("Message")
        return str(inner) if inner else ""
    if value in (None, ""):
        return ""
    return str(value)


def _pick(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _error_detail(data: dict, http_status: int) -> str:
    flat = _unwrap(data)
    for key in ("error_message", "ErrorMessage", "message", "msg", "error",
                "Error", "detail"):
        value = flat.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("msg")
        if value:
            return str(value)
    code = _pick(flat, "error_code", "ErrorCode", "code")
    if code:
        return str(code)
    return f"HTTP {http_status}"


def _request(method: str, path: str, *, json_body: dict | None = None,
             retries: int = 3, timeout: int = 120) -> dict:
    url = path if path.startswith("http") else base_url() + path
    last_network_error: Exception | None = None

    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=_headers(),
                                    json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            last_network_error = exc
            time.sleep(2 ** attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            raise TokenHubError(
                f"腾讯 TokenHub 返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from None

        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 401:
                hint = "\n提示：检查 TOKENHUB_API_KEY 是否正确、是否已失效。"
            elif resp.status_code == 429:
                hint = "\n提示：触发限流。HumanActor 默认并发 5，隔几秒再试。"
            raise TokenHubError(
                f"腾讯 TokenHub 报错 (HTTP {resp.status_code}): "
                f"{_error_detail(data, resp.status_code)}{hint}"
            )
        return data if isinstance(data, dict) else {"data": data}

    raise TokenHubError(f"网络请求失败（已重试 {retries} 次）: {last_network_error}")


def recipe_spec(recipe: str) -> dict:
    spec = RECIPES.get(recipe)
    if spec is None:
        raise TokenHubError(
            f"未知腾讯方案 {recipe}。目前只接了：{', '.join(RECIPES)}"
        )
    return spec


def file_to_base64(path: str | Path) -> str:
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")


def public_url(source: str | Path) -> str:
    """本地文件变成 TokenHub 能拉到的公网 URL。已经是 URL 就原样返回。

    TokenHub 人像驱动的音频没有 Base64 字段，只要 URL。本地文件借 WaveSpeed
    的上传接口中转——调试台本来就配了海外 Key，不必再给腾讯单独开 COS。
    """
    source = str(source)
    if source.startswith(("http://", "https://")):
        return source
    path = Path(source)
    if not path.is_file():
        raise TokenHubError(f"找不到文件 {source}")
    try:
        import wsclient

        wsclient.api_key()
        return wsclient.upload(path)
    except Exception as exc:                      # noqa: BLE001
        raise TokenHubError(
            f"TokenHub 音频只要公网 URL，本地文件 {path.name} 传不上去。"
            f"配 WAVESPEED_API_KEY 会自动中转上传，或自己先把文件放到可访问的地址。"
            f"（{type(exc).__name__}: {exc}）"
        ) from exc


def build_payload(recipe: str, *, prompt: str, audio: str,
                  image: str = "", resolution: str = "",
                  logo_add: int = 0) -> dict:
    spec = recipe_spec(recipe)
    audio_url = public_url(audio)
    payload = {
        "model": spec["model"],
        "prompt": prompt,
        "audio_url": audio_url,
        "resolution": resolution or spec["resolution"],
        "frame_rate": spec["frame_rate"],
        "logo_add": logo_add,
    }
    if image:
        image = str(image)
        if image.startswith(("http://", "https://")):
            payload["image_url"] = image
        else:
            payload["image_base64"] = file_to_base64(image)
    return payload


def estimated_cny(recipe: str, seconds: float) -> float:
    spec = recipe_spec(recipe)
    credits = max(seconds, 0) * spec["credits_per_second"]
    return round(credits * YUAN_PER_CREDIT, 2)


def submit(payload: dict) -> str:
    """提交任务，返回任务 id。"""
    clean = {k: v for k, v in payload.items() if v is not None and v != ""}
    data = _unwrap(_request("POST", SUBMIT_PATH, json_body=clean))
    job_id = _pick(data, "id", "job_id", "JobId", "jobId", "task_id")
    if not job_id:
        raise TokenHubError(f"提交成功但没返回任务号: {data}")
    return str(job_id)


def query(model: str, job_id: str) -> dict:
    data = _unwrap(_request("POST", QUERY_PATH, json_body={
        "model": model,
        "id": job_id,
    }))
    status = str(_pick(data, "status", "Status") or "unknown")
    return {
        "status": status,
        "url": _pick(data, "result_video_url", "ResultVideoUrl",
                     "video_url", "url", "output"),
        "error_code": _as_text(_pick(data, "error_code", "ErrorCode")),
        "error_message": _as_text(_pick(data, "error_message", "ErrorMessage",
                                       "message", "error")),
        "raw": data,
    }


def poll(model: str, job_id: str, *, timeout: int = 3600, interval: int = 8,
         on_tick: Callable[[str, int], None] | None = None) -> dict:
    deadline = time.time() + timeout
    started = time.time()
    last = {}

    while time.time() < deadline:
        last = query(model, job_id)
        status = last["status"]
        elapsed = int(time.time() - started)
        if on_tick:
            on_tick(status, elapsed)

        if status in STATUS_OK:
            if not last.get("url"):
                raise TokenHubError(f"任务 {job_id} 显示完成但没有视频地址")
            return last
        if status in STATUS_BAD:
            detail = last.get("error_message") or last.get("error_code") or "无错误详情"
            raise TokenHubError(f"任务 {job_id} {status}: {detail}")
        time.sleep(interval)

    raise TokenHubError(
        f"任务 {job_id} 等待超过 {timeout}s 仍未完成（最后状态 {last.get('status')}）。"
        f"远端可能还在跑，可稍后重连取回。"
    )


def run(recipe: str, *, prompt: str, audio: str, image: str = "",
        timeout: int = 3600, interval: int = 8,
        on_submit: Callable[[str], None] | None = None,
        on_tick: Callable[[str, int], None] | None = None) -> dict:
    spec = recipe_spec(recipe)
    payload = build_payload(recipe, prompt=prompt, audio=audio, image=image)
    job_id = submit(payload)
    if on_submit:
        on_submit(job_id)
    result = poll(spec["model"], job_id, timeout=timeout, interval=interval,
                  on_tick=on_tick)
    result["id"] = job_id
    result["recipe"] = recipe
    return result


def download(url: str, output: str | Path, chunk: int = 1 << 20) -> Path:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(out, "wb") as f:
            for part in resp.iter_content(chunk_size=chunk):
                f.write(part)
    return out