"""本地文件输入解析：上传端点探测与 base64 编码。"""

from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

# 常见 ComfyUI / 代理上传端点（path, form field name）
IMAGE_UPLOAD_CANDIDATES: List[Tuple[str, str]] = [
    ("/api/comfy/proxy/upload/image", "image"),
    ("/api/comfy/upload/image", "image"),
    ("/api/comfy/upload/file", "file"),
    ("/api/comfy/upload/file", "image"),
    ("/api/upload/image", "image"),
    ("/upload/image", "image"),
]

VIDEO_UPLOAD_CANDIDATES: List[Tuple[str, str]] = [
    ("/api/comfy/proxy/upload/image", "image"),  # 部分代理统一走 image 端点
    ("/api/comfy/upload/video", "video"),
    ("/api/comfy/upload/file", "file"),
    ("/api/comfy/upload/file", "image"),
    ("/upload/video", "video"),
    ("/upload/image", "image"),
]


def guess_mime(path: str, *, kind: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime
    if kind == "video":
        return "video/mp4"
    if kind == "image":
        return "image/jpeg"
    return "application/octet-stream"


def encode_as_data_uri(path: str, *, kind: str) -> str:
    """把本地文件编码为 API 支持的 data URI。"""
    mime = guess_mime(path, kind=kind)
    with open(path, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def parse_upload_response(data: Dict[str, Any]) -> Optional[str]:
    """从上传响应中解析服务器端文件引用。"""
    if not isinstance(data, dict):
        return None

    # 直接字符串
    for key in ("name", "filename", "file", "path", "url"):
        val = data.get(key)
        if isinstance(val, str) and val:
            if key == "url":
                return val
            subfolder = data.get("subfolder") or ""
            return f"{subfolder}/{val}" if subfolder else val

    # 嵌套 data / result
    for key in ("data", "result"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = parse_upload_response(nested)
            if found:
                return found
    return None


def try_upload(
    session: requests.Session,
    *,
    base_url: str,
    local_path: str,
    kind: str,
    headers: Dict[str, str],
    timeout: float,
    candidates: List[Tuple[str, str]],
) -> Tuple[Optional[str], List[str]]:
    """依次尝试多个上传端点，返回 (引用, 错误日志)。"""
    errors: List[str] = []
    filename = os.path.basename(local_path)
    mime = guess_mime(local_path, kind=kind)

    # multipart 请求不要带 Accept: application/json，部分代理会异常
    upload_headers = {k: v for k, v in headers.items() if k.lower() != "accept"}

    for path, field in candidates:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            with open(local_path, "rb") as fh:
                files = {field: (filename, fh, mime)}
                data = {"type": "input", "subfolder": ""}
                resp = session.post(
                    url,
                    files=files,
                    data=data,
                    headers=upload_headers,
                    timeout=timeout,
                )
        except requests.RequestException as exc:
            errors.append(f"{url} ({field}): 网络错误 {exc}")
            continue

        if resp.status_code == 405:
            errors.append(f"{url} ({field}): 405 Method Not Allowed")
            continue
        if resp.status_code >= 400:
            errors.append(
                f"{url} ({field}): {resp.status_code} {resp.text[:200]}"
            )
            continue

        try:
            payload = resp.json()
        except ValueError:
            # 少数实现直接返回文件名字符串
            text = resp.text.strip()
            if text:
                return text.strip('"'), errors
            errors.append(f"{url} ({field}): 非 JSON 响应")
            continue

        ref = parse_upload_response(payload if isinstance(payload, dict) else {})
        if ref:
            return ref, errors
        errors.append(f"{url} ({field}): 响应缺少文件名 {payload!r}")

    return None, errors
