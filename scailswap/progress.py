"""进度回调：把长任务的阶段与百分比暴露给调用方（CLI / API / 前端轮询）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional


@dataclass
class ProgressEvent:
    """一次进度更新。percent 为全局 0~100。"""

    percent: float
    stage: str  # prepare | generate | assemble | audio | postprocess | done
    message: str
    chunk_index: Optional[int] = None
    chunks_total: Optional[int] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


ProgressCallback = Callable[[ProgressEvent], None]


class ProgressReporter:
    """把「阶段内进度」映射为全局百分比。

    默认权重：准备 0-2%，逐块生成 2-90%，融合拼接 90-95%，
    音频合并 95-98%，后处理/收尾 98-100%。
    """

    STAGES = {
        "prepare": (0.0, 2.0),
        "generate": (2.0, 90.0),
        "assemble": (90.0, 95.0),
        "audio": (95.0, 98.0),
        "postprocess": (98.0, 100.0),
    }

    def __init__(self, callback: Optional[ProgressCallback] = None) -> None:
        self._cb = callback
        self._last_percent = 0.0

    def report(
        self,
        stage: str,
        fraction: float,
        message: str,
        chunk_index: Optional[int] = None,
        chunks_total: Optional[int] = None,
        **extra: object,
    ) -> None:
        lo, hi = self.STAGES.get(stage, (0.0, 100.0))
        percent = lo + max(0.0, min(1.0, fraction)) * (hi - lo)
        # 保证百分比单调不回退（重试时不给用户"倒退"的观感）
        percent = max(percent, self._last_percent)
        self._last_percent = percent
        if self._cb:
            self._cb(
                ProgressEvent(
                    percent=round(percent, 2),
                    stage=stage,
                    message=message,
                    chunk_index=chunk_index,
                    chunks_total=chunks_total,
                    extra=dict(extra),
                )
            )

    def done(self, message: str = "全部完成") -> None:
        self._last_percent = 100.0
        if self._cb:
            self._cb(ProgressEvent(percent=100.0, stage="done", message=message))
