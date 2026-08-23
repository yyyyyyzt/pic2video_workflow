#!/usr/bin/env python3
"""数字人成片的客观评测。

和 compliance.py 分工明确：

    compliance.py   能不能入库——平台的硬门槛（时长/分辨率/帧率/静默头）。
                    二值判定，标准来自腾讯/阿里的文档，不需要讨论。
    videometrics/   好不好——角色塑造质量。连续值，标准需要用你的主观选择来校准。

维度沿用 AGI-Eval《2026 数字人生成评测报告》的四维骨架（合理性 / 协调性 /
稳定性 / 一致性），因为那套是 770 人主观打分 + 10 名专家逐帧标注归纳出来的，
比自己拍脑袋分类可靠。另加两维：

  体态   肩线、体型比例、颈部纹理、手的高度。按使用方反馈这一维优先级最高——
         「不会盯着嘴型看，看整体自然才重要，包括动作和肢体」。
  活性   眨眼、静默段的冻结与吸气。静默段已确认不参与训练，所以那部分只记录
         不定级；口播段一次不眨眼仍然算问题。

一个必须说清楚的前提：**这些指标不是「质量分」，而是「缺陷探测器」**。
指标高不代表好看（正对镜头一动不动的脸口型分最高，但很呆——AGI-Eval 报告里
Gemini-Omni 就是这个现象）。所以默认只输出向量和缺陷时间线，不给总分；
总分要等有了你的主观标注再拟合，见 docs/EVALUATION.md。
"""

from __future__ import annotations

from .aliveness import aliveness_metrics
from .anatomy import anatomy_metrics
from .facetrack import (FaceTrack, MetricsUnavailable, quiet_backends,
                        track_faces)
from .identity import identity_metrics
from .lipsync import audio_envelope, lipsync_metrics
from .plausibility import plausibility_metrics
from .report import evaluate, evaluate_to_dict
from .stability import stability_metrics

__all__ = [
    "FaceTrack",
    "MetricsUnavailable",
    "aliveness_metrics",
    "anatomy_metrics",
    "audio_envelope",
    "evaluate",
    "evaluate_to_dict",
    "identity_metrics",
    "lipsync_metrics",
    "plausibility_metrics",
    "quiet_backends",
    "stability_metrics",
    "track_faces",
]
