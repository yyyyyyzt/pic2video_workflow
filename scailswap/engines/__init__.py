"""引擎工厂。"""

from __future__ import annotations

from typing import Optional

from ..config import Settings, load_settings
from ..errors import InvalidInputError
from .base import ChunkProgress, ChunkTask, Engine


def create_engine(
    name: Optional[str] = None,
    settings: Optional[Settings] = None,
    output_dir: Optional[str] = None,
) -> Engine:
    """按名称创建引擎：comfyui（默认，支持长视频锚定）/ fal（短片）/ fake（调试）。"""
    settings = settings or load_settings()
    name = (name or settings.engine or "comfyui").lower()
    chunks_dir = output_dir or f"{settings.data_dir}/chunks"

    if name == "comfyui":
        from .comfyui_engine import ComfyUIEngine

        return ComfyUIEngine(settings.comfyui, output_dir=chunks_dir)
    if name == "fal":
        from .fal_engine import FalEngine

        return FalEngine(settings.fal, output_dir=chunks_dir)
    if name == "fake":
        from .fake_engine import FakeEngine

        return FakeEngine(output_dir=chunks_dir)
    raise InvalidInputError(f"未知引擎：{name!r}（可选 comfyui / fal / fake）")


__all__ = ["ChunkProgress", "ChunkTask", "Engine", "create_engine"]
