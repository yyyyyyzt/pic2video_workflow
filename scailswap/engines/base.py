"""生成引擎抽象层。

引擎只做一件事：给定「参考图 + 一段驱动视频 + 可选锚点帧」，产出这一段的
替换/迁移结果。分块调度、融合、音频等全部在 processor 层，引擎无状态可替换。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ChunkTask:
    """一次单块生成请求。"""

    index: int
    driving_video: str          # 本块驱动视频（已裁切到 gen_length 帧，4n+1）
    reference_image: str        # 源角色照片
    gen_length: int             # 期望生成帧数（与 driving_video 帧数一致）
    width: int
    height: int
    fps: float                  # 分块视频帧率（与源一致）
    prompt: str
    negative_prompt: str
    seed: int
    steps: int
    cfg: float
    shift: float
    mode: str = "replacement"   # replacement | animation
    # —— 长视频锚定（关键）——
    # 上一块的输出视频路径；引擎取其末尾 anchor_frames 帧作为 previous_frames，
    # 模型将这些帧 VAE 编码后冻结为新块 latent 头部，实现模型级语义衔接。
    anchor_video: Optional[str] = None
    anchor_frames: int = 5
    # SAM3 跟踪目标（开放词汇文本）
    video_object: str = "person"
    image_object: str = "person"
    max_objects: int = 1
    extra: dict = field(default_factory=dict)


# 单块内部进度：fraction 0~1 + 文本说明
ChunkProgress = Callable[[float, str], None]


class Engine(ABC):
    """生成引擎接口。"""

    name: str = "base"
    #: 是否支持 previous_frames 模型级锚定。不支持的引擎无法安全生成多块长视频。
    supports_anchor: bool = False

    @abstractmethod
    def generate_chunk(self, task: ChunkTask, on_progress: Optional[ChunkProgress] = None) -> str:
        """执行单块生成，返回本地输出视频路径（帧数 == task.gen_length）。

        失败时抛 EngineError；显存溢出抛 EngineOOMError（由 processor 清显存重试）。
        """

    def free_memory(self, aggressive: bool = False) -> None:
        """释放推理端显存。

        - aggressive=False：清理缓存（等价 torch.cuda.empty_cache()），每块之后调用；
        - aggressive=True：连模型权重一起卸载，OOM 重试前调用。
        """

    def health_check(self) -> dict:
        """返回引擎可用性信息（API 健康检查用）。"""
        return {"engine": self.name, "ok": True}
