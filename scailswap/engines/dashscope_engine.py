"""阿里云百炼（DashScope）API 引擎。

支持三个模型，锚定能力差异很大，直接决定能不能做长视频：

============================  ==========  ==============  ==================================
模型                          单次时长    锚定能力        说明
============================  ==========  ==============  ==================================
``wan2.2-animate-mix``        2~30s       NONE            视频换人。只吃 image+video，
                                                          无任何跨块锚定接口 → 多块必拒
``wan2.2-animate-move``       2~30s       NONE            动作迁移（背景来自参考图）
``wan2.7-videoedit``          2~10s       REFERENCE       指令编辑 + 最多 4 张参考图，
                                                          可做参考级伪锚定 → 支持长视频
============================  ==========  ==============  ==================================

wan2.7 的长视频思路（实验性）
============================
wan2.7 是**通用视频编辑**模型，角色替换只是它"局部替换"能力的一种用法：
传入驱动视频 + 参考图 + 指令（如"把视频里的人替换为参考图中的人"）。它不像
Wan2.2-Animate 那样有 ``continue_motion`` 这类 latent 锚点输入，但它接受
**最多 4 张参考图**——这留出了一条弱锚定通路：

    第 1 块：参考图 = [源角色照片]
    第 i 块：参考图 = [源角色照片, 第 i-1 块输出的末帧, （可选）更早的锚帧]

模型因此能"看到"上一块里角色最终长成什么样，身份漂移显著小于完全独立生成。
但要清楚它的**本质局限**：新块的开头是模型重新生成的像素，与上一块结尾并非
同一批像素，所以 processor 会把 overlap 强制为 0（做 crossfade 只会产生鬼影），
块边界仍可能有轻微跳变。这条路适合用来快速验证 wan2.7 的画质上限，
不能替代自托管的 latent 级锚定。

计费与限制
==========
- 异步接口，必须带 ``X-DashScope-Async: enable``；
- 输出帧率由服务端固定（animate 系列 std=15fps / pro=25fps），**不跟随源视频**。
  processor 的 ``target_fps`` 会在拼接前把各块统一重采样到目标帧率；
- 按生成秒数计费，失败不计费；
- 素材需要可访问的 URL 或 base64 data URI。大视频建议配 ``DASHSCOPE_PUBLIC_BASE_URL``
  走公网静态目录，否则回退 base64（有大小上限）。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import time
import uuid
from typing import List, Optional

import requests

from ..config import DashScopeConfig
from ..errors import EngineError, InvalidInputError
from .base import AnchorMode, ChunkProgress, ChunkTask, Engine

#: 各模型的单次时长上限（秒）与锚定能力
_MODEL_SPECS = {
    "wan2.2-animate-mix": {"max_seconds": 30.0, "anchor": AnchorMode.NONE, "kind": "animate"},
    "wan2.2-animate-move": {"max_seconds": 30.0, "anchor": AnchorMode.NONE, "kind": "animate"},
    "wan2.7-videoedit": {"max_seconds": 10.0, "anchor": AnchorMode.REFERENCE, "kind": "videoedit"},
}

_ANIMATE_PATH = "/api/v1/services/aigc/image2video/video-synthesis"
_VIDEOEDIT_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
_TASK_PATH = "/api/v1/tasks/{task_id}"


def _data_uri(path: str, max_bytes: int) -> str:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise EngineError(
            f"{os.path.basename(path)} 体积 {size / 1048576:.1f}MB 超过 base64 上限 "
            f"{max_bytes / 1048576:.0f}MB。请配置 DASHSCOPE_PUBLIC_BASE_URL "
            "把素材目录暴露为公网 URL，或降低分辨率/缩短分块时长。"
        )
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        return f"data:{mime};base64,{base64.b64encode(fh.read()).decode()}"


class DashScopeEngine(Engine):
    """百炼托管 API 引擎。``anchor_mode`` / ``max_chunk_frames`` 随所选模型而变。"""

    name = "dashscope"

    def __init__(
        self,
        config: Optional[DashScopeConfig] = None,
        output_dir: str = "./data/chunks",
    ) -> None:
        self.cfg = config or DashScopeConfig()
        if not self.cfg.api_key:
            raise EngineError("使用 dashscope 引擎需要设置 DASHSCOPE_API_KEY")
        spec = _MODEL_SPECS.get(self.cfg.model)
        if spec is None:
            raise InvalidInputError(
                f"未知百炼模型 {self.cfg.model!r}，可选 {sorted(_MODEL_SPECS)}"
            )
        self.spec = spec
        self.anchor_mode = spec["anchor"]
        self.max_seconds = float(spec["max_seconds"])
        self.name = f"dashscope:{self.cfg.model}"
        self.output_dir = output_dir
        self.base_url = self.cfg.base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            }
        )

    # ------------------------------------------------------------------ #
    # 能力声明
    # ------------------------------------------------------------------ #
    def chunk_frames_limit(self, fps: float) -> int:
        """把服务端的秒数上限换算成帧数上限（留 1 帧余量避免边界被拒）。"""
        return max(4, int(self.max_seconds * fps) - 1)

    def health_check(self) -> dict:
        return {
            "engine": self.name,
            "ok": bool(self.cfg.api_key),
            "anchor_mode": self.anchor_mode.value,
            "model": self.cfg.model,
            "max_seconds": self.max_seconds,
            "note": (
                "wan2.7-videoedit 为参考级弱锚定（实验性）"
                if self.anchor_mode is AnchorMode.REFERENCE
                else "无跨块锚定，仅支持单块提交"
            ),
        }

    # ------------------------------------------------------------------ #
    # 素材寻址：公网 URL 优先，回退 base64
    # ------------------------------------------------------------------ #
    def _media_ref(self, local_path: str) -> str:
        if self.cfg.public_base_url and self.cfg.public_root:
            abs_path = os.path.abspath(local_path)
            root = os.path.abspath(self.cfg.public_root)
            if abs_path.startswith(root + os.sep):
                rel = abs_path[len(root) + 1:].replace(os.sep, "/")
                return f"{self.cfg.public_base_url.rstrip('/')}/{rel}"
        return _data_uri(local_path, self.cfg.max_base64_bytes)

    # ------------------------------------------------------------------ #
    # 请求体构建
    # ------------------------------------------------------------------ #
    def _build_animate_payload(self, task: ChunkTask) -> tuple[str, dict]:
        model = self.cfg.model
        # mix=视频换人（保留原视频背景）；move=动作迁移（保留参考图背景）
        if task.mode == "replacement" and model.endswith("-move"):
            raise InvalidInputError(
                "replacement（换人）请使用 wan2.2-animate-mix；"
                "wan2.2-animate-move 用于 animation（动作迁移）"
            )
        payload = {
            "model": model,
            "input": {
                "image_url": self._media_ref(task.reference_image),
                "video_url": self._media_ref(task.driving_video),
            },
            "parameters": {
                "mode": self.cfg.service_mode,
                "check_image": self.cfg.check_image,
                "watermark": self.cfg.watermark,
            },
        }
        return _ANIMATE_PATH, payload

    def _build_videoedit_payload(self, task: ChunkTask) -> tuple[str, dict]:
        """wan2.7-videoedit：指令 + 参考图编辑。

        参考图列表 = [源角色照片] + 锚点帧（上一块输出末帧）。服务端最多接受
        4 张，超出时保留最新的锚点（对身份延续贡献最大）。
        """
        media: List[dict] = [{"type": "video", "url": self._media_ref(task.driving_video)}]
        ref_paths = [task.reference_image, *task.anchor_images][: self.cfg.max_reference_images]
        for path in ref_paths:
            media.append({"type": "reference_image", "url": self._media_ref(path)})

        prompt = task.prompt or self.cfg.default_edit_prompt
        payload = {
            "model": self.cfg.model,
            "input": {"prompt": prompt, "media": media},
            "parameters": {
                # duration=0 表示保留输入视频完整时长（我们已按上限切好块）
                "duration": 0,
                "resolution": self.cfg.resolution,
                # 保留原声：口播场景绝不能让模型重新生成音频
                "audio_setting": "origin",
                "prompt_extend": self.cfg.prompt_extend,
                "watermark": self.cfg.watermark,
            },
        }
        if task.negative_prompt:
            payload["input"]["negative_prompt"] = task.negative_prompt[:500]
        if task.seed is not None:
            payload["parameters"]["seed"] = int(task.seed) % 2147483648
        return _VIDEOEDIT_PATH, payload

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def generate_chunk(self, task: ChunkTask, on_progress: Optional[ChunkProgress] = None) -> str:
        def report(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        if task.anchor_video and self.anchor_mode is not AnchorMode.LATENT:
            raise EngineError(
                f"{self.name} 不支持 latent 级锚定（anchor_video），"
                "processor 应改用 anchor_images 传参考帧"
            )

        seconds = task.gen_length / max(task.fps, 1e-6)
        if seconds > self.max_seconds + 0.5:
            raise InvalidInputError(
                f"分块时长 {seconds:.1f}s 超过 {self.cfg.model} 上限 {self.max_seconds:.0f}s"
            )

        report(0.05, "构建请求并提交百炼任务…")
        if self.spec["kind"] == "animate":
            path, payload = self._build_animate_payload(task)
        else:
            path, payload = self._build_videoedit_payload(task)

        resp = self._session.post(
            f"{self.base_url}{path}", json=payload, timeout=self.cfg.submit_timeout
        )
        if resp.status_code not in (200, 201, 202):
            raise EngineError(
                f"百炼提交失败 HTTP {resp.status_code}: {resp.text[:600]}"
            )
        body = resp.json()
        task_id = (body.get("output") or {}).get("task_id")
        if not task_id:
            raise EngineError(f"百炼响应缺少 task_id：{resp.text[:400]}")

        video_url = self._poll_task(task_id, report)

        report(0.9, "下载生成结果…")
        os.makedirs(self.output_dir, exist_ok=True)
        dest = os.path.join(
            self.output_dir, f"ds_{task.index:04d}_{uuid.uuid4().hex[:6]}.mp4"
        )
        with self._session.get(video_url, stream=True, timeout=self.cfg.download_timeout) as dl:
            if dl.status_code != 200:
                raise EngineError(f"下载结果失败 HTTP {dl.status_code}")
            with open(dest, "wb") as fh:
                for block in dl.iter_content(1 << 20):
                    fh.write(block)
        report(1.0, "分块完成")
        return dest

    def _poll_task(self, task_id: str, report) -> str:
        url = f"{self.base_url}{_TASK_PATH.format(task_id=task_id)}"
        deadline = time.time() + self.cfg.task_timeout
        # 轮询用的请求头不能带 X-DashScope-Async
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        while time.time() < deadline:
            resp = self._session.get(url, headers=headers, timeout=60)
            if resp.status_code != 200:
                raise EngineError(f"查询任务失败 HTTP {resp.status_code}: {resp.text[:300]}")
            output = resp.json().get("output") or {}
            status = output.get("task_status")
            if status == "SUCCEEDED":
                video_url = output.get("video_url") or (output.get("results") or {}).get("video_url")
                if not video_url:
                    raise EngineError(f"任务成功但缺少 video_url：{resp.text[:400]}")
                return video_url
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise EngineError(
                    f"百炼任务 {status}：{output.get('message') or resp.text[:400]}"
                )
            report(0.5, f"百炼任务状态 {status}…")
            time.sleep(self.cfg.poll_interval)
        raise EngineError(f"百炼任务超时（>{self.cfg.task_timeout:.0f}s）task_id={task_id}")
