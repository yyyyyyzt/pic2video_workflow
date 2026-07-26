"""生成引擎抽象层。

引擎只做一件事：给定「参考图 + 一段驱动视频 + 可选锚点」，产出这一段的
替换/迁移结果。分块调度、融合、音频等全部在 processor 层，引擎无状态可替换。

锚定能力分三级（:class:`AnchorMode`），直接决定长视频的一致性上限，
processor 会据此自动调整分块与融合策略。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional


class AnchorMode(str, Enum):
    """跨块锚定的强度等级。

    LATENT
        **模型级锚定（最强）**。把上一块的解码输出送进模型，其末尾 N 帧被
        VAE 编码后冻结为新块 latent 的头部（不加噪、不重采样），模型在
        "已知开头"的条件下续写。ComfyUI 原生实现：SCAIL-2 的
        ``previous_frames`` 与 Wan2.2-Animate 的 ``continue_motion``。
        重叠区两侧内容几乎逐像素一致，因此可以安全做 crossfade 融合。

    REFERENCE
        **参考级锚定（弱，实验性）**。引擎不接受 latent 锚点，但接受额外的
        参考图；把上一块输出的末帧作为附加参考图传入，让模型"看到"上一块的
        角色外观。身份一致性明显优于完全无锚定，但新块开头是重新生成的，
        与上一块结尾**不是**同一批像素——因此 processor 会强制 overlap=0，
        避免 crossfade 产生鬼影（重影）。典型：百炼 ``wan2.7-videoedit``
        （支持最多 4 张参考图）。

    NONE
        **无锚定**。只能整段一次提交，多块长视频会被 processor 直接拒绝。
        典型：百炼 ``wan2.2-animate-mix``、fal.ai ``scail-2``。
    """

    LATENT = "latent"
    REFERENCE = "reference"
    NONE = "none"


@dataclass
class ChunkTask:
    """一次单块生成请求。"""

    index: int
    driving_video: str          # 本块驱动视频（已裁切到 gen_length 帧，4n+1）
    reference_image: str        # 源角色照片
    gen_length: int             # 期望生成帧数（与 driving_video 帧数一致）
    width: int
    height: int
    fps: float                  # 分块视频帧率（生成帧率）
    prompt: str
    negative_prompt: str
    seed: int
    steps: int
    cfg: float
    shift: float
    mode: str = "replacement"   # replacement | animation

    # —— 长视频锚定 ——
    # LATENT 模式：上一块的输出视频路径。引擎取其末尾 anchor_frames 帧作为
    # previous_frames / continue_motion，模型将其 VAE 编码后冻结为新块 latent
    # 头部，实现模型级语义衔接。
    anchor_video: Optional[str] = None
    anchor_frames: int = 5
    # REFERENCE 模式：附加参考图路径列表（通常是上一块输出的末帧），
    # 作为额外视觉证据帮助模型保持身份，但不冻结 latent。
    anchor_images: List[str] = field(default_factory=list)

    # SAM3 / 检测器的跟踪目标（开放词汇文本）
    video_object: str = "person"
    image_object: str = "person"
    max_objects: int = 1
    extra: dict = field(default_factory=dict)


# 单块内部进度：fraction 0~1 + 文本说明
ChunkProgress = Callable[[float, str], None]


class Engine(ABC):
    """生成引擎接口。"""

    name: str = "base"
    #: 锚定能力等级，决定长视频一致性上限
    anchor_mode: AnchorMode = AnchorMode.NONE
    #: 模型原生的分块窗口帧数（4n+1）。SCAIL-2=81，Wan2.2-Animate=77。
    native_window: int = 81
    #: 模型原生的跨块锚定帧数。两者都在 5 帧上训练。
    native_overlap: int = 5
    #: 单块可生成的最大帧数上限（None 表示等于 native_window）。
    #: 托管 API 按秒计费且有时长上限，会在引擎里换算成帧数填这里。
    max_chunk_frames: Optional[int] = None

    @property
    def supports_anchor(self) -> bool:
        """是否具备任意形式的跨块锚定（即能否生成多块长视频）。"""
        return self.anchor_mode is not AnchorMode.NONE

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
        return {"engine": self.name, "ok": True, "anchor_mode": self.anchor_mode.value}
