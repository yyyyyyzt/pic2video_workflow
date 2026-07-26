"""FakeEngine —— 无 GPU 的调试引擎（单元测试 / 管线演练用，不做真实生成）。

行为上模拟真实引擎的关键语义，让 LongVideoProcessor 的分块、锚定、融合、
帧率、音频链路可以在 CI 中被完整验证：

- 输出帧 = 驱动帧加一层可辨识的色调偏移（模拟"生成"）；
- LATENT 锚定：输出的前 overlap 帧**直接复用锚点视频末尾帧**（模拟
  ``continue_motion`` / ``previous_frames`` 冻结 latent 的效果）；
- REFERENCE 锚定：只记录收到的 ``anchor_images``，不复用像素（真实的参考级
  锚定同样无法保证像素一致），用于验证 processor 会正确切到不重叠模式。
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

import numpy as np

from ..video_io import read_frames, write_chunk_video
from .base import AnchorMode, ChunkProgress, ChunkTask, Engine


class FakeEngine(Engine):
    name = "fake"
    anchor_mode = AnchorMode.LATENT
    native_window = 81
    native_overlap = 5

    def __init__(
        self,
        output_dir: str = "./data/chunks",
        tint: int = 30,
        anchor_mode: AnchorMode = AnchorMode.LATENT,
        native_window: int = 81,
        native_overlap: int = 5,
    ) -> None:
        self.output_dir = output_dir
        self.tint = tint
        self.anchor_mode = anchor_mode
        self.native_window = native_window
        self.native_overlap = native_overlap
        self.calls: list[ChunkTask] = []  # 供测试断言

    def generate_chunk(self, task: ChunkTask, on_progress: Optional[ChunkProgress] = None) -> str:
        self.calls.append(task)
        if on_progress:
            on_progress(0.5, "fake 引擎生成中")
        frames = read_frames(task.driving_video)
        out = []
        for f in frames:
            g = f.astype(np.int16)
            g[..., 1] = np.clip(g[..., 1] + self.tint, 0, 255)  # 绿色通道偏移标记
            out.append(g.astype(np.uint8))

        if task.anchor_video and task.anchor_frames > 0:
            anchor = read_frames(task.anchor_video)
            tail = anchor[-task.anchor_frames:]
            for i, fr in enumerate(tail[: len(out)]):
                h, w = out[i].shape[:2]
                if fr.shape[:2] != (h, w):
                    import cv2

                    fr = cv2.resize(fr, (w, h))
                out[i] = fr

        os.makedirs(self.output_dir, exist_ok=True)
        dest = os.path.join(self.output_dir, f"fake_{task.index:04d}_{uuid.uuid4().hex[:6]}.mp4")
        write_chunk_video(out, dest, fps=task.fps, lossless=True)
        if on_progress:
            on_progress(1.0, "fake 分块完成")
        return dest
