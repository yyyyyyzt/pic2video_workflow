#!/usr/bin/env python3
"""模力方舟（Gitee AI / moark.com）Serverless API 客户端 —— 国内节点备选通道。

为什么值得单独接一条：它的模型清单里同时有 InfiniteTalk 和 Duix-Avatar，
是国内唯一同时覆盖「单图直出」和「口型替换」两条路线的聚合商，
而且节点在国内，调用延迟和合规性都比走海外好。

协议（异步任务，官方文档 https://moark.com/docs/products/apis/async-task）：
    POST /v1/async/videos/generations   提交，返回 task_id
    GET  /v1/task/{task_id}             查询，成功后 data.file_url 是结果
    GET  /v1/task/{task_id}/status      只查状态
    POST /api/v1/task/{task_id}/cancel  取消
    请求头 X-WebHook 可指定回调地址

注意：InfiniteTalk / Duix-Avatar 在模力方舟上的确切入参（字段名、时长上限、单价）
需要登录控制台在模型体验页看「API」示例才能确认，官网模型列表是前端动态加载的，
未登录抓不到。所以下面的 payload 字段名按平台通用约定写，
第一次调用请用 `python3 moarkclient.py probe` 打印真实报错来校准。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import requests

BASE_URL = "https://api.moark.com"
SUBMIT_PATH = "/v1/async/videos/generations"

# 模力方舟上与数字人相关的模型（取自其公开的 /v1/models 清单）
KNOWN_MODELS = {
    "InfiniteTalk": "单图 + 音频直出说话视频，长视频一致性好",
    "Duix-Avatar": "口型替换，需要真人模板视频 + 音频",
    "Wan2_2-I2V-A14B": "图生视频，可用来做静默素材",
    "HunyuanVideo-1.5": "腾讯混元视频，和腾讯数智人同源",
    "ViduQ3-Pro": "Vidu Q3 视频生成",
    "seedance-2.5": "字节 Seedance 2.5",
}


class MoarkError(Exception):
    pass


def api_key() -> str:
    key = os.environ.get("MOARK_API_KEY", "").strip()
    if not key:
        raise MoarkError(
            "未配置 MOARK_API_KEY（见 .env.example）。\n"
            "  在 https://moark.com 工作台 → 设置 → 访问令牌 创建，\n"
            "  并确认令牌已授权到你购买的资源包。"
        )
    return key


def base_url() -> str:
    return os.environ.get("MOARK_API_BASE", BASE_URL).rstrip("/")


def _request(method: str, path: str, *, json_body: dict | None = None,
             webhook: str = "", retries: int = 3, timeout: int = 120) -> dict:
    url = path if path.startswith("http") else base_url() + path
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    if webhook:
        headers["X-WebHook"] = webhook
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(2 ** attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            raise MoarkError(f"接口返回非 JSON (HTTP {resp.status_code}): {resp.text[:300]}") from None

        if resp.status_code >= 400 or data.get("error"):
            raise MoarkError(
                f"模力方舟报错 (HTTP {resp.status_code}): "
                f"{data.get('error') or data.get('message') or data}"
            )
        return data

    raise MoarkError(f"网络请求失败（已重试 {retries} 次）: {last_error}")


def list_models() -> list[str]:
    """公开的模型清单，不需要鉴权也能拿到。"""
    resp = requests.get(f"{base_url()}/v1/models", timeout=30)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]


def submit(model: str, payload: dict, *, webhook: str = "") -> str:
    """提交异步视频任务，返回 task_id。"""
    body = {"model": model, **{k: v for k, v in payload.items() if v not in (None, "")}}
    data = _request("POST", SUBMIT_PATH, json_body=body, webhook=webhook)
    task_id = data.get("task_id") or (data.get("data") or {}).get("task_id")
    if not task_id:
        raise MoarkError(f"提交成功但没拿到 task_id，返回：{data}")
    return task_id


def poll(task_id: str, *, timeout: int = 3600, interval: int = 10,
         on_tick: Callable[[str, int], None] | None = None) -> dict:
    deadline = time.time() + timeout
    started = time.time()

    while time.time() < deadline:
        data = _request("GET", f"/v1/task/{task_id}")
        payload = data.get("data") or data
        status = str(payload.get("status", "unknown")).lower()
        if on_tick:
            on_tick(status, int(time.time() - started))

        if status in ("success", "succeeded", "completed", "finished"):
            url = payload.get("file_url") or (payload.get("result") or {}).get("file_url")
            if not url:
                raise MoarkError(f"任务 {task_id} 完成但没有 file_url：{payload}")
            return {"status": status, "url": url}
        if status in ("failed", "error", "canceled", "cancelled"):
            raise MoarkError(f"任务 {task_id} {status}: {payload.get('message') or payload}")

        time.sleep(interval)

    raise MoarkError(f"任务 {task_id} 等待超过 {timeout}s 仍未完成")


def run(model: str, payload: dict, *, timeout: int = 3600, interval: int = 10,
        on_tick: Callable[[str, int], None] | None = None) -> dict:
    task_id = submit(model, payload)
    result = poll(task_id, timeout=timeout, interval=interval, on_tick=on_tick)
    result["task_id"] = task_id
    return result


def cancel(task_id: str) -> dict:
    return _request("POST", f"/api/v1/task/{task_id}/cancel")


def quota() -> dict:
    return _request("GET", "/v1/tasks/available-quota")


def download(url: str, output: str | Path, chunk: int = 1 << 20) -> Path:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(out, "wb") as f:
            for part in resp.iter_content(chunk_size=chunk):
                f.write(part)
    return out


def _cli() -> None:
    """`probe` 子命令用来校准入参：拿真实报错反推平台要什么字段。"""
    import argparse
    import json

    from wsclient import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="模力方舟（国内）通道自检")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("models", help="列出平台模型（无需鉴权）")
    p.add_argument("--filter", default="")

    p = sub.add_parser("probe", help="试探某模型的入参，用报错反推字段名")
    p.add_argument("model", help="例如 InfiniteTalk / Duix-Avatar")
    p.add_argument("--image", default="")
    p.add_argument("--audio", default="")
    p.add_argument("--video", default="")

    sub.add_parser("quota", help="查配额")

    args = parser.parse_args()

    if args.cmd == "models":
        names = list_models()
        hits = [n for n in names if args.filter.lower() in n.lower()]
        for n in sorted(hits):
            print(f"  {n:<32}{KNOWN_MODELS.get(n, '')}")
        print(f"\n共 {len(hits)} / {len(names)} 个模型")
        return

    if args.cmd == "quota":
        print(json.dumps(quota(), ensure_ascii=False, indent=2))
        return

    payload = {k: v for k, v in
               (("image", args.image), ("audio", args.audio), ("video", args.video)) if v}
    print(f"提交 {args.model}，payload={payload}")
    try:
        print("task_id:", submit(args.model, payload))
    except MoarkError as exc:
        print(f"\n报错内容就是校准依据，据此调整字段名：\n{exc}")


if __name__ == "__main__":
    _cli()
