#!/usr/bin/env python3
"""WaveSpeedAI API 共享核心：鉴权、文件上传、异步任务提交与轮询。

与 klingclient.py 的定位一致：出错一律 raise WaveSpeedError，不调用 sys.exit，
CLI 和 Web 服务都能复用。

WaveSpeed 的所有模型走同一套异步协议，只是 api_path 和 payload 不同：
    POST /api/v3/{model_id}              提交任务，返回 data.id
    GET  /api/v3/predictions/{id}/result 轮询，status 走 created→processing→completed
    POST /api/v3/media/uploads           申请上传票据，再 PUT 文件字节
    GET  /api/v3/balance                 查余额
"""

from __future__ import annotations

import mimetypes
import os
import time
from pathlib import Path
from typing import Callable

import requests

BASE_URL = "https://api.wavespeed.ai"
UPLOAD_MAX_MB = 200

# 视频类模型按「每 5 秒」计费，base_price 就是单个 5 秒块的价格。
# 例：infinitetalk base_price=0.15 → $0.03/秒 → 60 秒 12 块 = $1.80
BILLING_BLOCK_SECONDS = 5

TERMINAL_OK = "completed"
TERMINAL_BAD = {"failed", "error", "canceled", "cancelled"}


class WaveSpeedError(Exception):
    """所有可预期的失败（配置缺失、参数不合法、API 报错、任务失败）。"""


def api_key() -> str:
    key = os.environ.get("WAVESPEED_API_KEY", "").strip()
    if not key:
        raise WaveSpeedError(
            "未配置 WAVESPEED_API_KEY（见 .env.example）。\n"
            "  在 https://wavespeed.ai 控制台创建，形如 wsk_live_xxxx"
        )
    return key


def base_url() -> str:
    return os.environ.get("WAVESPEED_API_BASE", BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


def _request(method: str, path: str, *, json_body: dict | None = None,
             retries: int = 3, timeout: int = 120) -> dict:
    """带重试的 API 调用。网络抖动重试，业务错误直接抛。"""
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
            raise WaveSpeedError(
                f"接口返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from None

        if resp.status_code != 200 or data.get("code") not in (0, 200):
            message = data.get("message", "")
            hint = ""
            if resp.status_code == 401:
                hint = "\n提示：检查 WAVESPEED_API_KEY 是否正确、是否已失效。"
            elif "balance" in str(message).lower() or "insufficient" in str(message).lower():
                hint = "\n提示：余额不足。先用 `python3 avatar_lab.py budget` 估算成本再跑。"
            elif resp.status_code == 429:
                hint = ("\n提示：触发限流。Bronze 账号只有 5 次/分钟、2 个并发，"
                        "用 --concurrency 1 并加大 --interval。")
            raise WaveSpeedError(
                f"WaveSpeed 报错 (HTTP {resp.status_code}) code={data.get('code')}: {message}{hint}"
            )
        return data

    raise WaveSpeedError(f"网络请求失败（已重试 {retries} 次）: {last_network_error}")


def balance() -> float:
    """账户余额（美元）。"""
    return float(_request("GET", "/api/v3/balance")["data"]["balance"])


def upload(source: str | Path) -> str:
    """本地文件 → WaveSpeed CDN 公网 URL；已经是 URL 则原样返回。

    WaveSpeed 的模型输入只吃公网 URL，不接受裸 base64，所以本地素材必须先上传。
    走官方推荐的两步式直传：申请票据 → PUT 字节，文件不经过 API 网关。
    """
    source = str(source)
    if source.startswith(("http://", "https://")):
        return source

    path = Path(source)
    if not path.is_file():
        raise WaveSpeedError(f"找不到文件 {source}")

    size = path.stat().st_size
    if size > UPLOAD_MAX_MB * 1024 * 1024:
        raise WaveSpeedError(f"文件 {size / 1024 / 1024:.1f}MB 超过 {UPLOAD_MAX_MB}MB 上限")

    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    ticket = _request("POST", "/api/v3/media/uploads", json_body={
        "filename": path.name,
        "size": size,
        "content_type": content_type,
    })["data"]

    up = ticket["upload"]
    # 票据 URL 自带签名，绝对不能再带 Authorization 头，否则被存储端拒绝
    try:
        resp = requests.request(up.get("method", "PUT"), up["url"],
                                headers=up.get("headers") or {},
                                data=path.read_bytes(), timeout=600)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise WaveSpeedError(f"上传 {path.name} 失败: {exc}") from None

    return ticket["download_url"]


def submit(model_id: str, payload: dict) -> str:
    """提交任务，返回 prediction id。model_id 形如 wavespeed-ai/infinitetalk。"""
    clean = {k: v for k, v in payload.items() if v is not None and v != ""}
    data = _request("POST", f"/api/v3/{model_id}", json_body=clean)["data"]
    return data["id"]


def poll(prediction_id: str, *, timeout: int = 3600, interval: int = 10,
         on_tick: Callable[[str, int], None] | None = None) -> dict:
    """轮询到终态。返回 {status, outputs, error, inference_seconds}。

    长视频（120 秒素材）实测可能跑十几分钟，默认等 1 小时。
    """
    deadline = time.time() + timeout
    started = time.time()

    while time.time() < deadline:
        data = _request("GET", f"/api/v3/predictions/{prediction_id}/result")["data"]
        status = data.get("status", "unknown")
        elapsed = int(time.time() - started)

        if on_tick:
            on_tick(status, elapsed)

        if status == TERMINAL_OK:
            outputs = data.get("outputs") or []
            if not outputs:
                raise WaveSpeedError(f"任务 {prediction_id} 显示完成但没有输出")
            return {
                "status": status,
                "outputs": outputs,
                "error": "",
                "inference_seconds": float((data.get("timings") or {}).get("inference") or 0) / 1000,
            }
        if status in TERMINAL_BAD:
            raise WaveSpeedError(f"任务 {prediction_id} {status}: {data.get('error') or '无错误详情'}")

        time.sleep(interval)

    raise WaveSpeedError(
        f"任务 {prediction_id} 等待超过 {timeout}s 仍未完成。"
        f"任务还在跑，可用 `python3 avatar_lab.py fetch {prediction_id}` 稍后取回。"
    )


def run(model_id: str, payload: dict, *, timeout: int = 3600, interval: int = 10,
        on_tick: Callable[[str, int], None] | None = None) -> dict:
    """提交 + 轮询的组合，返回 poll() 的结果并附带 prediction_id。"""
    prediction_id = submit(model_id, payload)
    result = poll(prediction_id, timeout=timeout, interval=interval, on_tick=on_tick)
    result["prediction_id"] = prediction_id
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


CALIBRATION_PATH = Path("data/cost_calibration.json")


def load_calibration(path: Path = CALIBRATION_PATH) -> dict[str, dict]:
    """实测单价表：{model_id: {"per_second": float, "samples": int}}。

    WaveSpeed 的 base_price 单位不统一（有的每秒、有的每 5 秒），官方也声明以实际
    扣费为准，所以真实成本只能靠「调用前后的余额差」测出来。这张表由每次实跑
    自动累积，估算会越用越准。
    """
    if not path.is_file():
        return {}
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def record_calibration(model_id: str, cost: float, seconds: float,
                       path: Path = CALIBRATION_PATH) -> None:
    """把一次实测扣费并入单价表，取移动平均以摊掉四舍五入误差。"""
    if cost <= 0 or seconds <= 0:
        return
    import json

    table = load_calibration(path)
    entry = table.get(model_id, {"per_second": 0.0, "samples": 0})
    n = entry["samples"]
    entry["per_second"] = round((entry["per_second"] * n + cost / seconds) / (n + 1), 6)
    entry["samples"] = n + 1
    entry["last_cost"] = cost
    entry["last_seconds"] = round(seconds, 2)
    table[model_id] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")


def measured_per_second(model_id: str) -> float | None:
    entry = load_calibration().get(model_id)
    return entry["per_second"] if entry and entry.get("samples") else None


def estimate_cost(base_price: float, seconds: float) -> float:
    """按 5 秒块向上取整的粗估，仅在没有实测数据时兜底。"""
    blocks = max(1, -(-int(round(seconds)) // BILLING_BLOCK_SECONDS))
    return round(blocks * base_price, 4)


def list_models(refresh: bool = False, cache: str = "data/ws_models.json") -> list[dict]:
    """全量模型清单（含 base_price 与 api_schema），带本地缓存。"""
    import json

    cache_path = Path(cache)
    if not refresh and cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    data = _request("GET", "/api/v3/models", timeout=90)["data"]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data
