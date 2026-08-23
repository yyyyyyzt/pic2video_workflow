#!/usr/bin/env python3
"""体态与解剖合理性：整个人看起来对不对。

这一维是按实拍反馈加的，优先级最高。原话是「口型虽然重要，但在直播中并非
决定性问题，大家不会盯着嘴型看，看整体自然才重要，包括动作和肢体」，
以及一个很具体的故障：「有的模型会让脖子处本来不明显的骨骼变得格外显眼」。

AGI-Eval 的报告里这块叫「解剖质量」，由大模型按核查清单打分（手脚比例、
关节能不能正常弯曲、人物和背景融不融合）。我们不调大模型，用几何和纹理代理：

  肩线      两肩连线的倾角与长度。抖动、忽长忽短 = 身体在变形
  颈部纹理  颈部区域的边缘能量，除以脸颊区域做基准。
            突然升高就是「锁骨/颈筋被画出来了」——本来平滑的皮肤长出了结构。
  比例      肩宽 / 头高。生成模型在长视频里会慢慢改人的体型
  手部      手腕高度。平台要求手不抬到肩以上遮挡面部

注意颈部纹理比值是**相对量**：同一条视频内部随时间比较、以及同分辨率的
不同方案之间比较才有意义。384×576 和 768×1152 之间不能直接比。
"""

from __future__ import annotations

import numpy as np

# BlazePose 33 点里我们要用的几个
POSE_NOSE = 0
POSE_SHOULDER_L = 11
POSE_SHOULDER_R = 12
POSE_WRIST_L = 15
POSE_WRIST_R = 16
POSE_HIP_L = 23
POSE_HIP_R = 24

# 颈部边缘能量相对脸颊的倍数。绝对值受 ROI 位置和分辨率影响大，
# 只作描述用；判定改用下面的「时间维隆起」。**绝对阈值待校准**。
NECK_EDGE_RATIO_MAJOR = 2.2
NECK_EDGE_RATIO_MINOR = 1.6

# 颈部边缘能量在时间维上的隆起：p90 / 中位数。
# 用同一条视频内部的相对变化来判定，比跨视频比绝对值稳得多——
# 分辨率、肤色、光照都会平移绝对值，但不会平移「某几秒突然长出结构」这件事。
NECK_EDGE_RISE_MAJOR = 2.5
NECK_EDGE_RISE_MINOR = 1.7


def line_angle(x0: np.ndarray, y0: np.ndarray,
               x1: np.ndarray, y1: np.ndarray) -> np.ndarray:
    """两点连线相对水平的角度，单位度。用于肩线倾角。"""
    return np.degrees(np.arctan2(np.asarray(y1) - np.asarray(y0),
                                 np.asarray(x1) - np.asarray(x0)))


def robust_range(values: np.ndarray) -> float:
    """p95 − p5。比 max−min 抗单帧毛刺。"""
    x = np.asarray(values, dtype=float)
    finite = x[np.isfinite(x)]
    if finite.size < 4:
        return float("nan")
    return float(np.percentile(finite, 95) - np.percentile(finite, 5))


def anatomy_metrics(track, *, pose_stride: int = 5) -> dict:
    """体态指标。没有 pose 数据时只返回一条说明。

    pose 是抽帧算的，所以检测率要按**尝试过的帧**算，
    否则 stride=5 会永远显示 20%，看起来像 80% 的帧检不到人。
    """
    fps = max(track.fps, 1e-6)
    out: dict = {}

    shoulder_l_x = getattr(track, "shoulder_l_x", None)
    if shoulder_l_x is None or not np.isfinite(shoulder_l_x).any():
        out["note"] = "没有身体关键点（缺 pose 模型或画面里看不到肩部），体态未评估"
        return out

    lx, ly = track.shoulder_l_x, track.shoulder_l_y
    rx, ry = track.shoulder_r_x, track.shoulder_r_y
    valid = np.isfinite(lx) & np.isfinite(ly) & np.isfinite(rx) & np.isfinite(ry)
    attempted = max(int(np.ceil(track.frame_count / max(pose_stride, 1))), 1)
    out["pose_frames_sampled"] = int(valid.sum())
    out["pose_detect_rate"] = round(min(float(valid.sum()) / attempted, 1.0), 4)

    if valid.sum() >= 8:
        angle = line_angle(lx, ly, rx, ry)
        out["shoulder_tilt_std"] = round(float(np.nanstd(angle)), 3)
        out["shoulder_tilt_range"] = round(robust_range(angle), 3)

        width = np.hypot(rx - lx, ry - ly)
        finite_width = width[np.isfinite(width)]
        if finite_width.size:
            mean_w = float(finite_width.mean())
            out["shoulder_width_mean"] = round(mean_w, 4)
            # 归一化的宽度波动：身体在长视频里被慢慢改形状时这个会涨
            out["shoulder_width_cv"] = (
                round(float(finite_width.std() / mean_w), 4) if mean_w > 1e-6 else None)
            out["shoulder_width_range_norm"] = (
                round(robust_range(width) / mean_w, 4) if mean_w > 1e-6 else None)

        # 肩宽 / 头高：体型比例。缓慢单向变化说明人在长视频里被改体型
        head = track.face_size
        both = valid & np.isfinite(head) & (head > 1e-6)
        if both.sum() >= 8:
            ratio = np.where(both, width / np.where(head > 1e-6, head, np.nan), np.nan)
            finite_ratio = ratio[np.isfinite(ratio)]
            out["shoulder_head_ratio_mean"] = round(float(finite_ratio.mean()), 4)
            out["shoulder_head_ratio_std"] = round(float(finite_ratio.std()), 4)
            slope = np.polyfit(np.arange(finite_ratio.size), finite_ratio, 1)[0]
            out["shoulder_head_ratio_drift"] = round(
                float(slope) * (finite_ratio.size - 1), 4)

    neck = getattr(track, "neck_edge_ratio", None)
    if neck is not None and np.isfinite(neck).any():
        finite_neck = neck[np.isfinite(neck)]
        median = float(np.median(finite_neck))
        p90 = float(np.percentile(finite_neck, 90))
        out["neck_edge_ratio_median"] = round(median, 3)
        out["neck_edge_ratio_p90"] = round(p90, 3)
        if median > 1e-6 and finite_neck.size >= 4:
            out["neck_edge_rise"] = round(p90 / median, 3)
        # 峰值出现在第几秒，方便直接跳过去看
        worst = int(np.nanargmax(np.where(np.isfinite(neck), neck, -np.inf)))
        out["neck_edge_worst_at_s"] = round(worst / fps, 2)

    wrist = getattr(track, "wrist_above_shoulder", None)
    if wrist is not None and np.isfinite(wrist).any():
        frac = float(np.nanmean(wrist))
        out["hand_above_shoulder_frac"] = round(frac, 4)

    return out
