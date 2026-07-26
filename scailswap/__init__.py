"""scailswap —— 基于 SCAIL-2 的长视频角色替换核心库。

对外主要暴露：

- :class:`LongVideoProcessor`：长视频分块 + 模型级锚定 + 融合拼接的编排器；
- :func:`swap_character`：一行完成"照片 + 参考视频 → 替换后长视频"的门面函数；
- :func:`create_engine`：按配置创建生成引擎（comfyui / fal / fake）。
"""

from .chunking import ChunkPlanner, ChunkSpec
from .engines import create_engine
from .errors import EngineError, EngineOOMError, ScailSwapError
from .facade import swap_character
from .processor import LongVideoProcessor, ProcessorParams
from .progress import ProgressEvent

__all__ = [
    "ChunkPlanner",
    "ChunkSpec",
    "EngineError",
    "EngineOOMError",
    "LongVideoProcessor",
    "ProcessorParams",
    "ProgressEvent",
    "ScailSwapError",
    "create_engine",
    "swap_character",
]

__version__ = "1.0.0"
