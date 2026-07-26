"""门面函数：一行完成「照片 + 参考视频 → 替换后长视频」。"""

from __future__ import annotations

from typing import Optional

from .engines import create_engine
from .processor import LongVideoProcessor, ProcessorParams
from .progress import ProgressCallback


def swap_character(
    source_image: str,
    target_video: str,
    output_path: str = "output.mp4",
    prompt: str = "",
    engine: Optional[str] = None,
    params: Optional[ProcessorParams] = None,
    on_progress: Optional[ProgressCallback] = None,
    **overrides,
) -> str:
    """把 ``source_image`` 中的角色替换进 ``target_video``，输出长视频。

    ``overrides`` 直接覆盖 :class:`ProcessorParams` 字段，常用的有
    ``target_fps``（生成帧率，口播推荐 16）、``output_fps``（成片帧率）、
    ``mode``、``seed``、``resolution_tier``。

    Examples
    --------
    >>> from scailswap import swap_character
    >>> swap_character("face.jpg", "talk.mp4", "final.mp4",
    ...                prompt="一位穿深色衬衫的男士坐在办公室里对着镜头讲话",
    ...                target_fps=16, output_fps=30)
    """
    p = params or ProcessorParams()
    if prompt:
        p.prompt = prompt
    for key, value in overrides.items():
        if not hasattr(p, key):
            raise TypeError(f"未知参数：{key}")
        setattr(p, key, value)
    eng = create_engine(engine)
    processor = LongVideoProcessor(eng, p)
    return processor.process(
        source_image=source_image,
        driving_video=target_video,
        output_path=output_path,
        on_progress=on_progress,
    )
