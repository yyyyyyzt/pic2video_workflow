#!/usr/bin/env python3
"""模力方舟（Gitee AI）Serverless API 客户端 —— 国内通道。

`api.moark.com/v1` 和 `ai.gitee.com/v1` 是同一个后端，两个都能用（实测同一个
token 在两边拿到完全一样的资源包余额）。默认走 api.moark.com。

**端点和单价不写死，从平台自己的模型清单里读。**
`GET /v1/models?include_details=true` 每个模型都带 operations 数组：

    {"id": "Duix-Avatar",
     "operations": [{"type": "audio_video2video", "name": "数字人生成",
                     "path": "v1/async/videos/audio-video-to-video",
                     "price": "0.0100", "unit_tag": {"name": "秒"}}]}

这比手写映射可靠得多——我手写的那版把数字人端点猜成 image-video-to-video，
实际是 audio-video-to-video，白试了好几轮。

需要手写的只有**文件字段名**，因为清单里没有。已实测确认（见 ENDPOINT_FILES）：

    image-to-video          image / image_url，InfiniteTalk 另需 cond_video + cond_audio
    audio-video-to-video    ref_audio + ref_video     ← 数字人（口型替换）
    image-video-to-video    ref_image + drive_video
    generations             纯文生用 prompt；图生/参考生要 content[] + 文件走 multipart
    generations/multimodal  必填 content[]（text / image_url / video_url / audio_url）

三个踩过的坑：

1. **文件类参数必须走 multipart，写在 JSON 里会被网关静默丢掉。**
   JSON 模式只认 image_url 这种 URL 字段。`content` 也是文件类字段：
   塞进 application/json 体会被网关丢掉，然后报「缺少必填字段 'content'」。
   正确做法是 submit 里让 content 走 multipart（JSON 数组编成一个
   content.json 部件），本地图/音/视频另附在 files 里。

2. **状态词是 failure 不是 failed。** 完整取值：
   waiting → in_progress → success / failure / cancelled
   按 "failed" 判终态的客户端会在失败任务上一直轮询到超时。

3. **清单里的 price 不一定准。** Duix-Avatar 标 ¥0.01/秒，
   实测 6 秒扣 ¥0.6 = ¥0.1/秒，差 10 倍。所以 price 只作参考，
   真实成本看任务返回里的 price 字段。

返回体形状：
    {"task_id": "...", "status": "success",
     "output": {"file_url": "..."} 或 {"error": {"code":500,"message":"..."}},
     "price": 0.6, "currency": "CNY",
     "created_at": ..., "started_at": ..., "completed_at": ...}
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Callable

import requests

BASE_URL = "https://api.moark.com/v1"
MODEL_CACHE = Path("data/moark_models.json")

# 端点按输入类型分，不按模型分
EP_TEXT_TO_VIDEO = "/async/videos/generations"
EP_MULTIMODAL = "/async/videos/generations/multimodal"
EP_IMAGE_TO_VIDEO = "/async/videos/image-to-video"
EP_IMAGE_VIDEO_TO_VIDEO = "/async/videos/image-video-to-video"
EP_AUDIO_VIDEO_TO_VIDEO = "/async/videos/audio-video-to-video"

STATUS_OK = {"success", "succeeded", "completed", "finished"}
STATUS_BAD = {"failure", "failed", "error", "cancelled", "canceled"}

# 每个端点要哪些文件字段。清单里没有这个信息，只能靠报错反推，
# 下面每一条都是实测确认过的。(必填文件, 可选文件)
ENDPOINT_FILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    EP_IMAGE_TO_VIDEO: (("image",), ("audio", "cond_video", "cond_audio")),
    EP_AUDIO_VIDEO_TO_VIDEO: (("ref_audio", "ref_video"), ()),
    EP_IMAGE_VIDEO_TO_VIDEO: (("ref_image", "drive_video"), ()),
    EP_TEXT_TO_VIDEO: ((), ("image", "audio", "video")),
    EP_MULTIMODAL: ((), ("image", "audio", "video")),
}

# 语义角色 → 各端点的实际字段名。调用方只说「这是角色图/驱动音频/模板视频」，
# 具体叫什么由这里翻译，免得每处都记一遍字段名。
ROLE_FIELDS: dict[str, dict[str, str]] = {
    EP_IMAGE_TO_VIDEO: {"image": "image", "audio": "cond_audio", "video": "cond_video"},
    EP_AUDIO_VIDEO_TO_VIDEO: {"audio": "ref_audio", "video": "ref_video"},
    EP_IMAGE_VIDEO_TO_VIDEO: {"image": "ref_image", "video": "drive_video"},
    # generations / multimodal 的文件挂在 content[] 旁边，字段名保持语义名，
    # 真正的必填项是 content 数组，由 compose_multimodal() 组。
    EP_TEXT_TO_VIDEO: {"image": "image", "audio": "audio", "video": "video"},
    EP_MULTIMODAL: {"image": "image", "audio": "audio", "video": "video"},
}

# 这些端点用 content[] 描述多模态输入，而不是 image / ref_audio 那种扁平字段。
CONTENT_ENDPOINTS = {EP_TEXT_TO_VIDEO, EP_MULTIMODAL}

# 平台清单里 operation.name 带这些字，说明 generations 端点其实能吃图/参考素材。
_I2V_OP_MARKERS = ("图生", "参考", "首尾", "首帧")

# 实测跑通过的模型的补充说明。端点和单价都从平台清单读，这里只记
# 清单里读不到、但用的人必须知道的事。
NOTES: dict[str, dict] = {
    "Duix-Avatar": {
        "available": True,
        "note": "口型替换。要 ref_audio + ref_video（模板视频），不是单张图。"
                "实测 6 秒音频出 6.00 秒成片、扣 ¥0.6（清单标 ¥0.01/秒，实际约 ¥0.1/秒）。"
                "客观指标是目前测过里最好的：0 重缺陷、身份漂移 -0.02、肩线波动 3.9°。"
                "稳定性来自模板视频，所以模板拍一次就能反复用",
    },
    "LTX-2": {
        "available": True,
        "note": "图生视频，自带同步音频。实测扣 ¥1.5",
    },
    "Wan2_2-I2V-A14B": {
        "available": True,
        "note": "静默素材，无音频驱动。实测出 720p/15fps/无音轨、扣 ¥1.5，"
                "帧率达不到入库要求，只能看动作",
    },
    "seedance-2.0": {
        "available": None,
        "note": "多模态。必填 content[]，本地文件走 multipart。"
                "提示词不要只放 prompt 字段，网关会报缺少 content。",
    },
    "InfiniteTalk": {
        "available": False,
        "note": "实测平台侧返回 Service Temporarily Unavailable，后端未就绪。"
                "另外它要 cond_video（条件视频），不只是一张图",
    },
    "Duix.Heygem": {
        "available": False,
        "note": "实测返回「该模型已停用，请更换或升级为其他模型版本」",
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
                    "content[] 和本地图/音/视频都要用 submit(..., files={...})，"
                    "不要塞进 payload。")
        elif resp.status_code == 401:
            hint = "\n提示：检查 MOARK_API_KEY 是否有效、是否已授权到资源包。"
        raise MoarkError(f"模力方舟报错 (HTTP {resp.status_code}): {message}{hint}")
    return data


def list_models(*, timeout: int = 30) -> list[str]:
    """平台模型清单（只要名字）。不带鉴权也能拿到。"""
    resp = requests.get(f"{base_url()}/models", timeout=timeout)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", []) if isinstance(m, dict)]


def model_details(*, refresh: bool = False, timeout: int = 60,
                  cache: Path = MODEL_CACHE) -> list[dict]:
    """带 operations 的完整清单，本地缓存一份。

    operations 里有每个模型的端点、单价和计价单位，是端点映射的唯一可靠来源。
    """
    if not refresh and cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    data = _get_with_retry("/models?include_details=true", timeout=timeout)
    items = [m for m in (data.get("data") or []) if isinstance(m, dict)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


def _normalise_path(path: str) -> str:
    """清单里的 path 形如 "v1/async/videos/..."，转成本客户端用的 "/async/..."。"""
    cleaned = "/" + str(path or "").strip().lstrip("/")
    return cleaned[3:] if cleaned.startswith("/v1/") else cleaned


def video_models(*, refresh: bool = False) -> dict[str, dict]:
    """能出视频的模型 → {endpoint, price, unit, kind, label, files, note, available}。

    只保留 operations 落在本客户端支持的端点上的那些，其余（文生图、OCR、
    向量之类）过滤掉。
    """
    out: dict[str, dict] = {}
    for model in model_details(refresh=refresh):
        model_id = model.get("id")
        if not model_id:
            continue
        for op in model.get("operations") or []:
            endpoint = _normalise_path(op.get("path", ""))
            if endpoint not in ENDPOINT_FILES:
                continue
            required, optional = ENDPOINT_FILES[endpoint]
            unit = ((op.get("unit_tag") or {}).get("name")) or "次"
            try:
                price = float(op.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            meta = NOTES.get(model_id, {})
            op_names = [str(item.get("name") or "") for item in (model.get("operations") or [])]
            i2v_via_content = (
                endpoint in CONTENT_ENDPOINTS
                and any(any(mark in name for mark in _I2V_OP_MARKERS) for name in op_names)
            )
            entry = {
                "model": model_id,
                "endpoint": endpoint,
                "kind": op.get("type") or "",
                "op_label": op.get("name") or "",
                "price": price,
                "unit": unit,
                "files": list(required),
                "optional_files": list(optional),
                "roles": ROLE_FIELDS.get(endpoint, {}),
                "i2v_via_content": i2v_via_content,
                "available": meta.get("available"),
                "note": meta.get("note", ""),
                "description": (model.get("description") or "")[:160],
            }
            # 一个模型可能挂多个 operation，优先留数字人那种
            previous = out.get(model_id)
            if previous is None or _kind_rank(entry["kind"]) < _kind_rank(previous["kind"]):
                out[model_id] = entry
    return out


def _kind_rank(kind: str) -> int:
    """挑 operation 的优先级：数字人 > 图生视频 > 多模态 > 其它。"""
    order = ("audio_video2video", "image2video", "multimodal_video",
             "image_video2video", "text2video")
    return order.index(kind) if kind in order else len(order)


def avatar_models(*, refresh: bool = False) -> dict[str, dict]:
    """只要能做数字人的：音频驱动、图生视频，以及 generations 上挂着图生/参考生的。"""
    return {k: v for k, v in video_models(refresh=refresh).items()
            if v["kind"] in ("audio_video2video", "image2video", "multimodal_video")
            or v.get("i2v_via_content")}


def resolve_files(endpoint: str, *, image: str = "", audio: str = "",
                  video: str = "") -> tuple[dict[str, str], list[str]]:
    """把语义角色翻译成该端点的字段名，并指出还缺什么。

    返回 (可提交的文件字典, 缺失的必填字段名)。
    """
    roles = ROLE_FIELDS.get(endpoint, {})
    provided = {"image": image, "audio": audio, "video": video}
    files = {roles[role]: path for role, path in provided.items()
             if path and role in roles}
    required, _ = ENDPOINT_FILES.get(endpoint, ((), ()))
    return files, [f for f in required if f not in files]


def quota(*, timeout: int = 30) -> dict:
    """并发配额。{"available": 5, "max_concurrency": 5}"""
    return _get_with_retry("/tasks/available-quota", timeout=timeout)


def package_balance(*, timeout: int = 30) -> dict:
    """资源包余额（人民币）。

    {"total_amount": 30, "used_amount": 4.5, "balance": 25.5,
     "details": [{"ident": "...", "name": "全模型 Token 资源包", ...}]}
    """
    return _get_with_retry("/tokens/packages/balance", timeout=timeout)


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


def endpoint_for(model: str) -> str:
    """查这个模型该往哪个端点提交。查不到就退回图生视频。"""
    try:
        entry = video_models().get(model)
    except (MoarkError, requests.RequestException):
        entry = None
    return entry["endpoint"] if entry else EP_IMAGE_TO_VIDEO


def uses_content(endpoint: str, files: dict | None = None,
                 payload: dict | None = None) -> bool:
    """这个请求要不要组 content[]。

    generations 纯文生仍走 prompt JSON；一旦带了图/音/视频，或调用方已经给了
    content，就必须改走 content[] + multipart，否则网关报缺少 content。
    """
    if endpoint not in CONTENT_ENDPOINTS:
        return False
    if endpoint == EP_MULTIMODAL:
        return True
    if payload and payload.get("content"):
        return True
    return bool(files)


def compose_multimodal(*, prompt: str = "", image: str = "", audio: str = "",
                       video: str = "", duration: float | int | None = None,
                       extra: dict | None = None) -> tuple[dict, dict[str, str]]:
    """把提示词和本地/远程素材拼成 content[] + files。

    返回 (payload, files)。payload['content'] 是数组，submit() 会把它编成
    multipart 的 content.json 部件——直接 json= 会让网关把这个字段丢掉。
    本地文件另外放进 files（image / audio / video），不要写进 JSON。
    """
    content: list[dict] = []
    files: dict[str, str] = {}
    text = (prompt or "").strip()
    if text:
        content.append({"type": "text", "text": text})

    def add(path: str, url_key: str, role: str, file_key: str) -> None:
        if not path:
            return
        item: dict = {"type": url_key, "role": role}
        if str(path).startswith(("http://", "https://")):
            item[url_key] = {"url": path}
        else:
            files[file_key] = path
        content.append(item)

    # 只有角色图、没有模板视频时当首帧（图生）；有模板视频则当参考图。
    image_role = "first_frame" if image and not video else "reference_image"
    add(image, "image_url", image_role, "image")
    add(video, "video_url", "reference_video", "video")
    add(audio, "audio_url", "reference_audio", "audio")

    if not content:
        raise MoarkError(
            "多模态/图生端点必填 content：至少给一句提示词，或一张图 / 一段音视频。"
            "本地文件请走 files，不要写进 JSON payload。")

    payload: dict = {"content": content}
    if text:
        payload["prompt"] = text
    if duration:
        try:
            payload["duration"] = int(duration)
        except (TypeError, ValueError):
            pass
    for key, value in (extra or {}).items():
        if value is not None and value != "" and key not in payload:
            payload[key] = value
    return payload, files


def _form_value(value) -> str:
    """multipart 里的标量。list/dict 编成 JSON 字符串，bool 小写。"""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _content_from_fields(fields: dict, multipart: dict) -> list[dict] | None:
    """submit 兜底：用 prompt 和已挂上的文件部件拼一份最小 content[]。"""
    parts: list[dict] = []
    prompt = fields.get("prompt") or ""
    if prompt:
        parts.append({"type": "text", "text": prompt})
    for key, url_key, role in (
        ("image", "image_url", "first_frame"),
        ("video", "video_url", "reference_video"),
        ("audio", "audio_url", "reference_audio"),
    ):
        if key in multipart:
            parts.append({"type": url_key, "role": role})
    return parts or None


def submit(model: str, payload: dict | None = None, *,
           files: dict[str, str] | None = None,
           endpoint: str | None = None,
           webhook: str = "", timeout: int = 300) -> str:
    """提交异步任务，返回 task_id。

    payload 放标量参数（prompt、num_frames 等）和 content[] 数组。
    files 放本地路径或 URL。有 files 或 content 数组就走 multipart——
    content 写进 JSON body 会被网关丢掉，报缺少必填字段 content。
    endpoint 不传就按模型清单查。
    """
    path = endpoint or endpoint_for(model)
    url = base_url() + path

    fields: dict[str, str] = {"model": model}
    content_obj = None
    for key, value in (payload or {}).items():
        if value is None or value == "":
            continue
        if key == "content" and isinstance(value, (list, dict)):
            content_obj = value
            continue
        fields[key] = _form_value(value)

    multipart = {k: _file_part(v) for k, v in (files or {}).items() if v}

    # 调用方忘了组 content[] 时兜底：否则网关直接报缺少必填字段 content。
    if content_obj is None and path in CONTENT_ENDPOINTS:
        if path == EP_MULTIMODAL or multipart:
            content_obj = _content_from_fields(fields, multipart)

    # content 是文件类字段：必须出现在 multipart 里。已有同名文件部件时
    # （调用方显式 files={'content': 图}）改把数组放到表单字段，避免盖掉。
    if content_obj is not None:
        encoded = json.dumps(content_obj, ensure_ascii=False)
        if "content" in multipart:
            fields["content"] = encoded
        else:
            multipart["content"] = (
                "content.json", encoded.encode("utf-8"), "application/json")

    headers = _headers()
    if webhook:
        headers["X-WebHook"] = webhook

    if multipart:
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


def _error_detail(data: dict) -> str:
    """从失败任务里挖出人能看懂的原因。

    output.error 两种形状都出现过：
        {"error": {"code": 500, "message": "Service Temporarily Unavailable"}}
        {"error": "An unexpected error has occurred, please check the server log."}
    直接 .get("message") 会在第二种上抛 AttributeError。
    """
    output = data.get("output") or {}
    error = output.get("error") if isinstance(output, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    if isinstance(error, str) and error.strip():
        return error
    return str(data.get("message") or data.get("status") or "无错误详情")


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
            raise MoarkError(f"任务 {task_id} {status}: {_error_detail(data)}")

        time.sleep(interval)

    raise MoarkError(
        f"任务 {task_id} 等待超过 {timeout}s 仍未完成。"
        f"任务还在跑，可用 `python3 moarkclient.py task {task_id}` 稍后取回。")


def run(model: str, payload: dict | None = None, *,
        files: dict[str, str] | None = None, endpoint: str | None = None,
        timeout: int = 3600,
        interval: int = 10, on_tick: Callable[[str, int], None] | None = None,
        on_submit: Callable[[str], None] | None = None) -> dict:
    """提交并等待。

    on_submit 一拿到 task_id 就回调。调用方**务必**把它记下来：任务在服务端
    要跑几分钟，这期间本地进程挂了/网断了，只要有 task_id 就还能把结果取回来，
    没有就等于钱白花了。
    """
    task_id = submit(model, payload, files=files, endpoint=endpoint)
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

    p = sub.add_parser("known", help="列出能做数字人的模型：端点、单价、必填字段")
    p.add_argument("--refresh", action="store_true", help="强制重新拉取模型清单")

    sub.add_parser("quota", help="查并发配额")
    sub.add_parser("balance", help="查资源包余额（人民币）")

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
            local = NOTES.get(n)
            mark = "" if not local else ("  ← 已校准" if local["available"]
                                        else "  ← 已校准（平台侧不可用）")
            print(f"  {n}{mark}")
        print(f"\n共 {len(hits)} / {len(names)} 个模型")
        return

    if args.cmd == "known":
        found = avatar_models(refresh=args.refresh)
        for name, spec in sorted(found.items(),
                                 key=lambda kv: (kv[1]["available"] is False,
                                                 kv[1]["kind"], kv[0])):
            state = {True: "实测可用", False: "平台侧不可用", None: "未测"}[spec["available"]]
            print(f"\n{name}　[{state}]　{spec['op_label']}")
            print(f"  端点   {spec['endpoint']}")
            print(f"  单价   ¥{spec['price']:g}/{spec['unit']}")
            print(f"  必填文件 {', '.join(spec['files']) or '无'}")
            if spec["roles"]:
                mapping = "  ".join(f"{k}→{v}" for k, v in spec["roles"].items())
                print(f"  角色映射 {mapping}")
            if spec["note"]:
                print(f"  备注   {spec['note']}")
        print(f"\n共 {len(found)} 个。端点和单价读自平台清单，不是写死的。")
        return

    if args.cmd == "quota":
        print(json.dumps(quota(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "balance":
        data = package_balance()
        print(f"资源包余额 ¥{data.get('balance')}　"
              f"总额 ¥{data.get('total_amount')}　已用 ¥{data.get('used_amount')}")
        for item in data.get("details") or []:
            print(f"  {item.get('name')}　余 ¥{item.get('balance')} / ¥{item.get('amount')}")
        return

    if args.cmd == "task":
        print(json.dumps(task(args.task_id), ensure_ascii=False, indent=2))
        return

    spec = avatar_models().get(args.model) or video_models().get(args.model)
    if spec and spec["available"] is False:
        print(f"提醒：{args.model} 上次实测平台侧不可用（{spec['note']}），仍然继续尝试。")

    endpoint = spec["endpoint"] if spec else endpoint_for(args.model)
    if uses_content(endpoint, files={"image": args.image, "audio": args.audio,
                                     "video": args.video} if (args.image or args.audio or args.video) else None):
        payload, file_map = compose_multimodal(
            prompt=args.prompt, image=args.image, audio=args.audio,
            video=args.video)
    else:
        file_map, missing = resolve_files(endpoint, image=args.image,
                                          audio=args.audio, video=args.video)
        if missing:
            raise SystemExit(
                f"错误：{args.model} 走 {endpoint} 还缺 {', '.join(missing)}。\n"
                f"  该端点的角色映射：{ROLE_FIELDS.get(endpoint, {})}")
        payload = {"prompt": args.prompt}

    print(f"提交 {args.model} → {endpoint}，prompt={args.prompt[:40]!r}，"
          f"文件={ {k: Path(v).name for k, v in file_map.items()} }")
    result = run(args.model, payload, files=file_map,
                 endpoint=endpoint,
                 on_submit=lambda t: print(f"  任务号 {t}（失败可凭它重查）"),
                 on_tick=lambda s, e: print(f"  {s} ({e}s)"))
    print(f"完成：{result['url']}")
    if result.get("price") is not None:
        print(f"扣费：{result['price']} {result['currency']}")
    if args.output:
        print(f"已下载 {download(result['url'], args.output)}")


if __name__ == "__main__":
    _cli()
