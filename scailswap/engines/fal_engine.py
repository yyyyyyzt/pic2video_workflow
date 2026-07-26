"""fal.ai 托管 API 引擎（可选，短片/快速验证用）。

⚠️ 重要限制：fal.ai 的 ``fal-ai/scail-2`` 端点**不暴露 previous_frames 输入**，
无法做跨块模型级锚定（supports_anchor=False）。因此 processor 只会把**整段
视频一次性**提交给该引擎，不做分块——适合 ≤81 帧（约 3~5 秒）的快速验证，
或服务端内部支持的时长上限内的短片。1~2 分钟长视频请使用 comfyui 引擎。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from typing import Optional

import requests

from ..config import FalConfig
from ..errors import EngineError
from .base import ChunkProgress, ChunkTask, Engine

_QUEUE_BASE = "https://queue.fal.run"


def _data_uri(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode()
    return f"data:{mime};base64,{payload}"


class FalEngine(Engine):
    name = "fal"
    supports_anchor = False

    def __init__(self, config: Optional[FalConfig] = None, output_dir: str = "./data/chunks"):
        self.cfg = config or FalConfig()
        if not self.cfg.api_key:
            raise EngineError("使用 fal 引擎需要设置 FAL_KEY 环境变量")
        self.output_dir = output_dir
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Key {self.cfg.api_key}"

    def health_check(self) -> dict:
        return {"engine": self.name, "ok": bool(self.cfg.api_key), "model": self.cfg.model_id}

    def generate_chunk(self, task: ChunkTask, on_progress: Optional[ChunkProgress] = None) -> str:
        def report(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        if task.anchor_video:
            raise EngineError("fal 引擎不支持 previous_frames 锚定，无法生成多块长视频")

        report(0.05, "提交 fal.ai 任务…")
        payload = {
            "image_url": _data_uri(task.reference_image),
            "video_url": _data_uri(task.driving_video),
            "prompt": task.prompt,
            "mode": "replacement" if task.mode == "replacement" else "animation",
        }
        if task.seed is not None:
            payload["seed"] = task.seed
        submit = self._session.post(
            f"{_QUEUE_BASE}/{self.cfg.model_id}", json=payload, timeout=300
        )
        if submit.status_code not in (200, 201, 202):
            raise EngineError(f"fal 提交失败 HTTP {submit.status_code}: {submit.text[:500]}")
        request_id = submit.json().get("request_id")
        if not request_id:
            raise EngineError(f"fal 响应缺少 request_id: {submit.text[:300]}")

        status_url = f"{_QUEUE_BASE}/{self.cfg.model_id}/requests/{request_id}/status"
        result_url = f"{_QUEUE_BASE}/{self.cfg.model_id}/requests/{request_id}"
        deadline = time.time() + self.cfg.timeout
        while time.time() < deadline:
            status = self._session.get(status_url, timeout=60).json()
            state = status.get("status")
            if state == "COMPLETED":
                break
            if state in ("FAILED", "CANCELLED"):
                raise EngineError(f"fal 任务失败：{status}")
            report(0.5, f"fal 任务状态：{state}（队列位置 {status.get('queue_position', '-')}）")
            time.sleep(self.cfg.poll_interval)
        else:
            raise EngineError(f"fal 任务超时（>{self.cfg.timeout:.0f}s）")

        result = self._session.get(result_url, timeout=120).json()
        video = result.get("video") or {}
        url = video.get("url")
        if not url:
            raise EngineError(f"fal 结果缺少视频 URL：{str(result)[:500]}")

        report(0.9, "下载 fal 结果…")
        os.makedirs(self.output_dir, exist_ok=True)
        dest = os.path.join(self.output_dir, f"fal_{request_id}.mp4")
        resp = self._session.get(url, stream=True, timeout=600)
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for block in resp.iter_content(1 << 20):
                fh.write(block)
        report(1.0, "完成")
        return dest
