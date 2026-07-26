"""分块规划（采样器）—— 长视频时间一致性的第一步。

为什么不能简单切成不重叠的片段？
================================
SCAIL-2 训练时的时序上下文是「81 帧窗口 + 5 帧重叠（步进 76）」。要让模型在
生成第 i+1 段时"记得"第 i 段的角色状态，必须：

1. 相邻分块共享 ``overlap`` 帧**源内容**（驱动视频的同一批帧）；
2. 把第 i 段**生成结果**的末尾 ``overlap`` 帧作为 ``previous_frames`` 锚点传给
   第 i+1 段，模型将其 VAE 编码后冻结为新段 latent 的头部（不加噪、不重采样），
   在"已知开头"的条件下续写后面的新帧。

本模块只负责第 1 步的数学：把总帧数切成满足模型约束（帧数 4n+1、窗口上限）
的分块序列。锚定与融合分别在 engines / blending 中完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .errors import InvalidInputError

# SCAIL-2 的训练配置：81 帧窗口 / 5 帧重叠 / 76 帧步进。
DEFAULT_WINDOW = 81
DEFAULT_OVERLAP = 5


def ceil_to_4n1(n: int) -> int:
    """向上取整到最近的 4n+1（Wan VAE 时间压缩 4:1 的硬约束）。"""
    if n <= 1:
        return 1
    return ((n - 2) // 4 + 1) * 4 + 1


@dataclass(frozen=True)
class ChunkSpec:
    """一个分块的完整描述。

    Attributes
    ----------
    index:
        分块序号（0 起）。
    src_start / src_end:
        本块覆盖的源视频帧区间 [src_start, src_end)。第 i 块（i>0）的前
        ``overlap`` 帧与第 i-1 块的末尾 ``overlap`` 帧是**同一批源帧**。
    overlap:
        与上一块共享的帧数（第 0 块为 0）。
    gen_length:
        提交给模型的生成帧数（4n+1 对齐）。若源帧不足则通过复制末帧补齐，
        生成后再裁回真实帧数。
    pad_frames:
        为满足 4n+1 而补充的尾部帧数（= gen_length - (src_end - src_start)）。
    """

    index: int
    src_start: int
    src_end: int
    overlap: int
    gen_length: int
    pad_frames: int

    @property
    def src_length(self) -> int:
        return self.src_end - self.src_start

    @property
    def new_frames(self) -> int:
        """本块为最终视频贡献的"新"帧数（去掉与上一块的重叠）。"""
        return self.src_length - self.overlap


class ChunkPlanner:
    """按「窗口 + 重叠」规划分块序列。

    Parameters
    ----------
    window:
        每块最大帧数，需满足 4n+1。默认 81（模型训练值，不建议修改）。
    overlap:
        相邻块共享帧数，需满足 4n+1（VAE 时间压缩后恰为整数个 latent 帧）。
        默认 5（模型训练值）。
    """

    def __init__(self, window: int = DEFAULT_WINDOW, overlap: int = DEFAULT_OVERLAP) -> None:
        if window < 5 or (window - 1) % 4 != 0:
            raise InvalidInputError(f"window 必须为 4n+1 且 >=5，当前 {window}")
        if overlap < 1 or (overlap - 1) % 4 != 0:
            raise InvalidInputError(f"overlap 必须为 4n+1 且 >=1，当前 {overlap}")
        if overlap >= window:
            raise InvalidInputError(f"overlap({overlap}) 必须小于 window({window})")
        self.window = window
        self.overlap = overlap

    @property
    def stride(self) -> int:
        return self.window - self.overlap

    def plan(self, total_frames: int) -> List[ChunkSpec]:
        """把 ``total_frames`` 帧规划为分块序列。

        规划保证：
        - 所有源帧都被至少一个分块覆盖（无丢帧，输出时长 == 输入时长）；
        - 第 i 块（i>0）与第 i-1 块重叠恰好 ``overlap`` 帧源内容；
        - 每块提交给模型的帧数为 4n+1（尾块不足时记录 pad_frames，由调用方
          复制末帧补齐、生成后裁回）。
        """
        if total_frames <= 0:
            raise InvalidInputError("total_frames 必须为正数")

        chunks: List[ChunkSpec] = []
        start = 0
        index = 0
        while start < total_frames:
            remaining = total_frames - start
            # 尾部只剩重叠区（无新内容）时无需再生成一块
            if index > 0 and remaining <= self.overlap:
                break
            src_len = min(self.window, remaining)
            gen_len = ceil_to_4n1(src_len)
            chunks.append(
                ChunkSpec(
                    index=index,
                    src_start=start,
                    src_end=start + src_len,
                    overlap=self.overlap if index > 0 else 0,
                    gen_length=gen_len,
                    pad_frames=gen_len - src_len,
                )
            )
            if start + src_len >= total_frames:
                break
            start += self.stride
            index += 1
        return chunks
