#!/usr/bin/env python3
"""模力方舟（Gitee AI）Serverless API 客户端 —— 国内通道。

接口形状是拿真实 token 探出来的，不是照文档抄的（官方文档站是前端渲染，
抓不到内容）。以下每一条都实测确认过：

  Base            https://ai.gitee.com/v1        ← 不是 api.moark.com
  提交（JSON）    POST /v1/async/videos/generations        文生视频
  提交（表单）    POST /v1/async/videos/image-to-video     图生视频 / 数字人
                  POST /v1/async/videos/image-video-to-video
  查询            GET  /v1/task/{task_id}
  配额            GET  /v1/tasks/available-quota   → {"available":5,"max_concurrency":5}
  鉴权            Authorization: Bearer <token>

两个容易踩的坑：

1. **文件类参数必须走 multipart，写在 JSON 里会被网关静默丢掉。**
   实测：JSON 里传 cond_video 一直报「必传参数: cond_video」，
   同样的字段用 multipart 传就通过了。JSON 模式下只有 image_url 这种
   URL 形式的字段能用。

2. **状态词是 failure 不是 failed。** 完整取值：
   waiting → in_progress → success / failure / cancelled
   按 "failed" 判终态的客户端会在失败任务上一直轮询到超时。

返回体形状：
    {"task_id": "...", "status": "success",
     "output": {"file_url": "..."} 或 {"error": {"code":500,"message":"..."}},
     "price": 1.23, "currency": "CNY",
     "created_at": ..., "started_at": ..., "completed_at": ...}
"""

from __future__ import annotations

import mimetypes
import os
import time
from pathlib import Path
from typing import Callable

import requests

BASE_URL = "https://ai.gitee.com/v1"

# 端点按输入类型分，不按模型分
EP_TEXT_TO_VIDEO = "/async/videos/generations"
EP_IMAGE_TO_VIDEO = "/async/videos/image-to-video"
EP_IMAGE_VIDEO_TO_VIDEO = "/async/videos/image-video-to-video"

STATUS_OK = {"success", "succeeded", "completed", "finished"}
STATUS_BAD = {"failure", "failed", "error", "cancelled", "canceled"}

# 实测可用的数字人相关模型。available=False 的是探测时平台侧不可用，
# 保留在表里是为了给出准确的报错，而不是让人以为字段写错了。
MODELS: dict[str, dict] = {
    "InfiniteTalk": {
        "endpoint": EP_IMAGE_TO_VIDEO,
        "label": "InfiniteTalk 单图+音频说话",
        "files": ("image", "cond_video", "cond_audio"),
        "required": ("prompt", "image", "cond_video", "cond_audio"),
        "available": False,
        "note": "实测平台侧返回 Service Temporarily Unavailable，模型后端未就绪。"
                "注意它要 cond_video（条件视频）不只是一张图",
    },
    "Wan2_2-I2V-A14B": {
        "endpoint": EP_IMAGE_TO_VIDEO,
        "label": "Wan2.2 图生视频（静默素材）",
        "files": ("image",),
        "required": ("prompt", "image"),
        "available": True,
        "note": "实测可跑。没有音频驱动，出的是静默微动素材",
    },
    "Duix-Avatar": {
        "endpoint": EP_IMAGE_VIDEO_TO_VIDEO,
        "label": "Duix-Avatar 口型替换",
        "files": ("ref_image", "drive_video"),
        "required": ("ref_image", "drive_video"),
        "available": True,
        "note": "需要模板视频，只能走 multipart",
    },
    "Wan2.1-T2V-14B": {
        "endpoint": EP_TEXT_TO_VIDEO,
        "label": "Wan2.1 文生视频",
        "files": (),
        "required": ("prompt",),
        "available": True,
        "note": "纯文生，用不到角色图",
    },
}


class MoarkError(Exception):
    pass


def api_key() -> str:
    key = (os.environ.get("MOARK_API_KEY") or "").strip()
    if not key:
        raise MoarkError(
            "未配置 MOARK_API_KEY（见 .env.example）。\n"
            "  在 https://ai.gitee.com 工作台 → 设置 → 访问令牌 创建，\n"
            "  并确认令牌已授权到对应的资源包。"
        )
    return key


def base_url() -> str:
    return (os.environ.get("MOARK_API_BASE") or BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}"}


def _get_with_retry(path: str, *, retries: int = 4, timeout: int = 60) -> dict:
    """带退避重试的 GET。

    只对 GET 重试，POST 一律不重试——提交类请求在读超时后无法判断服务端是否
    已经建了任务，重试会重复提交、重复扣费。实测踩过的坑是反面：轮询没有重试，
    一次读超时就把整条任务判成失败，而远端其实还在跑，结果白花了钱又拿不到片子。
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return _unwrap(requests.get(base_url() + path, headers=_headers(),
                                        timeout=timeout))
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8))
    raise MoarkError(f"GET {path} 网络失败（已重试 {retries} 次）: {last}")


def _unwrap(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise MoarkError(
            f"接口返回非 JSON (HTTP {resp.status_code}): {resp.text[:300]}") from None
    if resp.status_code >= 400 or data.get("error"):
        message = data.get("message") or data.get("error") or data
        hint = ""
        if "必传参数" in str(message) or "缺少必填字段" in str(message):
            hint = ("\n提示：文件类参数必须走 multipart，写在 JSON 里会被网关丢掉。"
                    "用 submit(..., files={...}) 而不是塞进 payload。")
        elif resp.status_code == 401:
            hint = "\n提示：检查 MOARK_API_KEY 是否有效、是否已授权到资源包。"
        raise MoarkError(f"模力方舟报错 (HTTP {resp.status_code}): {message}{hint}")
    return data


def list_models(*, timeout: int = 30) -> list[str]:
    """平台模型清单。不带鉴权也能拿到。"""
    resp = requests.get(f"{base_url()}/models", timeout=timeout)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", []) if isinstance(m, dict)]


def quota(*, timeout: int = 30) -> dict:
    """并发配额。{"available": 5, "max_concurrency": 5}"""
    return _get_with_retry("/tasks/available-quota", timeout=timeout)


def _file_part(path_or_url: str) -> tuple[str, bytes, str]:
    """把本地路径或 URL 变成 multipart 的一个文件部件。"""
    name = Path(str(path_or_url).split("?")[0]).name or "upload.bin"
    if str(path_or_url).startswith(("http://", "https://")):
        resp = requests.get(path_or_url, timeout=120)
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("Content-Type") or "application/octet-stream"
    else:
        p = Path(path_or_url)
        if not p.is_file():
            raise MoarkError(f"找不到文件 {p}")
        content = p.read_bytes()
        content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return name, content, content_type


def submit(model: str, payload: dict | None = None, *,
           files: dict[str, str] | None = None,
           endpoint: str | None = None,
           webhook: str = "", timeout: int = 300) -> str:
    """提交异步任务，返回 task_id。

    payload 放标量参数（prompt、num_frames 等），files 放本地路径或 URL。
    有 files 就自动走 multipart。
    """
    spec = MODELS.get(model, {})
    path = endpoint or spec.get("endpoint") or EP_IMAGE_TO_VIDEO
    url = base_url() + path

    fields = {"model": model}
    for key, value in (payload or {}).items():
        if value is None or value == "":
            continue
        fields[key] = str(value).lower() if isinstance(value, bool) else str(value)

    headers = _headers()
    if webhook:
        headers["X-WebHook"] = webhook

    if files:
        multipart = {k: _file_part(v) for k, v in files.items() if v}
        resp = requests.post(url, headers=headers, data=fields,
                             files=multipart, timeout=timeout)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, json=fields, timeout=timeout)

    data = _unwrap(resp)
    task_id = data.get("task_id") or (data.get("data") or {}).get("task_id")
    if not task_id:
        raise MoarkError(f"提交成功但没拿到 task_id，返回：{data}")
    return task_id


def task(task_id: str, *, timeout: int = 60) -> dict:
    return _get_with_retry(f"/task/{task_id}", timeout=timeout)


def poll(task_id: str, *, timeout: int = 3600, interval: int = 10,
         on_tick: Callable[[str, int], None] | None = None) -> dict:
    deadline = time.time() + timeout
    started = time.time()

    while time.time() < deadline:
        data = task(task_id)
        status = str(data.get("status", "unknown")).lower()
        if on_tick:
            on_tick(status, int(time.time() - started))

        output = data.get("output") or {}
        if status in STATUS_OK:
            url = output.get("file_url") or output.get("url")
            if not url:
                raise MoarkError(f"任务 {task_id} 完成但没有 file_url：{data}")
            return {"status": status, "url": url,
                    "price": data.get("price"),
                    "currency": data.get("currency") or "CNY",
                    "task_id": task_id}
        if status in STATUS_BAD:
            error = output.get("error") or {}
            detail = error.get("message") or data.get("message") or status
            raise MoarkError(f"任务 {task_id} {status}: {detail}")

        time.sleep(interval)

    raise MoarkError(
        f"任务 {task_id} 等待超过 {timeout}s 仍未完成。"
        f"任务还在跑，可用 `python3 moarkclient.py task {task_id}` 稍后取回。")


def run(model: str, payload: dict | None = None, *,
        files: dict[str, str] | None = None, timeout: int = 3600,
        interval: int = 10, on_tick: Callable[[str, int], None] | None = None,
        on_submit: Callable[[str], None] | None = None) -> dict:
    """提交并等待。

    on_submit 一拿到 task_id 就回调。调用方**务必**把它记下来：任务在服务端
    要跑几分钟，这期间本地进程挂了/网断了，只要有 task_id 就还能把结果取回来，
    没有就等于钱白花了。
    """
    task_id = submit(model, payload, files=files)
    if on_submit:
        on_submit(task_id)
    return poll(task_id, timeout=timeout, interval=interval, on_tick=on_tick)


def cancel(task_id: str, *, timeout: int = 30) -> dict:
    return _unwrap(requests.post(f"{base_url()}/task/{task_id}/cancel",
                                 headers=_headers(), timeout=timeout))


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
    import argparse
    import json

    from wsclient import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(
        description="模力方舟（国内通道）客户端",
        epilog="接口形状是实测探出来的，见模块顶部注释")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("models", help="列出平台模型（无需鉴权）")
    p.add_argument("--filter", default="")

    sub.add_parser("known", help="列出本地已校准的数字人模型和它们的必填字段")
    sub.add_parser("quota", help="查并发配额")

    p = sub.add_parser("task", help="查任务状态")
    p.add_argument("task_id")

    p = sub.add_parser("run", help="跑一个模型")
    p.add_argument("model")
    p.add_argument("--prompt", default="")
    p.add_argument("--image", default="")
    p.add_argument("--audio", default="")
    p.add_argument("--video", default="")
    p.add_argument("-o", "--output", default="")

    args = parser.parse_args()

    if args.cmd == "models":
        names = list_models()
        hits = [n for n in names if args.filter.lower() in n.lower()]
        for n in sorted(hits):
            local = MODELS.get(n)
            mark = "" if not local else ("  ← 已校准" if local["available"]
                                        else "  ← 已校准（平台侧不可用）")
            print(f"  {n}{mark}")
        print(f"\n共 {len(hits)} / {len(names)} 个模型")
        return

    if args.cmd == "known":
        for name, spec in MODELS.items():
            state = "可用" if spec["available"] else "平台侧不可用"
            print(f"\n{name}　[{state}]　{spec['label']}")
            print(f"  端点   {spec['endpoint']}")
            print(f"  必填   {', '.join(spec['required'])}")
            print(f"  文件字段 {', '.join(spec['files']) or '无'}")
            if spec["note"]:
                print(f"  备注   {spec['note']}")
        return

    if args.cmd == "quota":
        print(json.dumps(quota(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "task":
        print(json.dumps(task(args.task_id), ensure_ascii=False, indent=2))
        return

    spec = MODELS.get(args.model)
    if spec and not spec["available"]:
        print(f"提醒：{args.model} 上次实测平台侧不可用（{spec['note']}），仍然继续尝试。")

    file_map = {}
    if spec:
        for field in spec["files"]:
            if field in ("image", "ref_image") and args.image:
                file_map[field] = args.image
            elif field in ("cond_audio", "audio") and args.audio:
                file_map[field] = args.audio
            elif field in ("cond_video", "drive_video") and args.video:
                file_map[field] = args.video
        # InfiniteTalk 的 cond_video 缺省用角色图兜底，方便先跑通链路
        if "cond_video" in spec["files"] and "cond_video" not in file_map and args.image:
            file_map["cond_video"] = args.image
    else:
        if args.image:
            file_map["image"] = args.image

    print(f"提交 {args.model}，prompt={args.prompt[:40]!r}，"
          f"文件={ {k: Path(v).name for k, v in file_map.items()} }")
    result = run(args.model, {"prompt": args.prompt}, files=file_map,
                 on_tick=lambda s, e: print(f"  {s} ({e}s)"))
    print(f"完成：{result['url']}")
    if result.get("price") is not None:
        print(f"扣费：{result['price']} {result['currency']}")
    if args.output:
        print(f"已下载 {download(result['url'], args.output)}")


if __name__ == "__main__":
    _cli()
