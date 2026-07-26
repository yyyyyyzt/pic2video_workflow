"""引擎工厂。

可选引擎与适用场景：

======================  ==========  ============================================
名称                    锚定能力    适用场景
======================  ==========  ============================================
``wan_animate``（默认） LATENT      自托管 Wan2.2-Animate。**口播/说话视频最优**
                                    （面部表情、口型有专门的条件通路）
``scail2``              LATENT      自托管 SCAIL-2。复杂 3D 动作、多人交互、
                                    非人角色更稳
``dashscope``           取决于模型  百炼 API。wan2.7-videoedit=REFERENCE（长视频
                                    实验性）；wan2.2-animate-*=NONE（≤30s 单块）
``fal``                 NONE        fal.ai 托管 SCAIL-2，≤81 帧短片验证
``fake``                LATENT      无 GPU 调试引擎（CI 测试用）
======================  ==========  ============================================
"""

from __future__ import annotations

from typing import Optional

from ..config import Settings, load_settings
from ..errors import InvalidInputError
from .base import AnchorMode, ChunkProgress, ChunkTask, Engine

#: 兼容旧名称：comfyui 曾专指 SCAIL-2 引擎
_ALIASES = {"comfyui": "scail2"}


def create_engine(
    name: Optional[str] = None,
    settings: Optional[Settings] = None,
    output_dir: Optional[str] = None,
) -> Engine:
    """按名称创建引擎。"""
    settings = settings or load_settings()
    name = (name or settings.engine or "wan_animate").lower()
    name = _ALIASES.get(name, name)
    chunks_dir = output_dir or f"{settings.data_dir}/chunks"

    if name == "wan_animate":
        from .wan_animate_engine import WanAnimateEngine

        return WanAnimateEngine(settings.wan_animate, output_dir=chunks_dir)
    if name == "scail2":
        from .comfyui_engine import ComfyUIEngine

        return ComfyUIEngine(settings.comfyui, output_dir=chunks_dir)
    if name == "dashscope":
        from .dashscope_engine import DashScopeEngine

        return DashScopeEngine(settings.dashscope, output_dir=chunks_dir)
    if name == "fal":
        from .fal_engine import FalEngine

        return FalEngine(settings.fal, output_dir=chunks_dir)
    if name == "fake":
        from .fake_engine import FakeEngine

        return FakeEngine(output_dir=chunks_dir)
    raise InvalidInputError(
        f"未知引擎：{name!r}（可选 wan_animate / scail2 / dashscope / fal / fake）"
    )


__all__ = ["AnchorMode", "ChunkProgress", "ChunkTask", "Engine", "create_engine"]
