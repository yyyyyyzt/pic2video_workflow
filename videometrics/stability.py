#!/usr/bin/env python3
"""稳定性：画面有没有抖、跳、漂色。

平台明确要求「全程无剪辑无跳帧」，而生成模型的典型故障恰好是这几种：

  跳帧     相邻帧差突然飙高（AGI-Eval 报告里 OmniHuman 在 13-14 秒跳帧就是这类）
  冻结     相邻帧差近零，画面卡住
  抖动     人脸中心位置的二阶差分偏大，看起来像手持
  漂色     整帧亮度单向走低/走高，长视频尾段发灰发暗

这些都从 facetrack 那一遍遍历里派生，不需要再读视频。
"""

from __future__ import annotations

import numpy as np


def jitter(series_x: np.ndarray, series_y: np.ndarray) -> dict:
    """人脸中心轨迹的抖动量，用二阶差分（加速度）而不是速度。

    匀速平移是正常的身体移动，突然改变方向才是抖。
    """
    x = np.asarray(series_x, dtype=float)
    y = np.asarray(series_y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 4:
        return {}
    x, y = x[mask], y[mask]
    accel = np.hypot(np.diff(x, 2), np.diff(y, 2))
    if accel.size == 0:
        return {}
    return {
        "accel_median": round(float(np.median(accel)), 6),
        "accel_p95": round(float(np.percentile(accel, 95)), 6),
        "accel_p99": round(float(np.percentile(accel, 99)), 6),
        "accel_max": round(float(accel.max()), 6),
    }


def find_spikes(diff: np.ndarray, *, factor: float = 6.0,
                min_abs: float = 4.0) -> list[int]:
    """找跳帧：帧差远高于自身中位数的位置。

    用中位数而不是均值做基线，避免几个大跳把阈值自己抬上去。
    """
    d = np.asarray(diff, dtype=float)
    finite = d[np.isfinite(d)]
    if finite.size < 8:
        return []
    base = float(np.median(finite))
    threshold = max(base * factor, min_abs)
    return [int(i) for i, v in enumerate(d) if np.isfinite(v) and v > threshold]


def find_freezes(diff: np.ndarray, *, threshold: float = 0.05,
                 min_run: int = 3) -> list[tuple[int, int]]:
    """找冻结段：连续多帧帧差近零。返回 [(起始下标, 长度)]。"""
    d = np.asarray(diff, dtype=float)
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(d):
        if np.isfinite(v) and v < threshold:
            start = i if start is None else start
        else:
            if start is not None and i - start >= min_run:
                runs.append((start, i - start))
            start = None
    if start is not None and len(d) - start >= min_run:
        runs.append((start, len(d) - start))
    return runs


def stability_metrics(track) -> dict:
    fps = max(track.fps, 1e-6)
    out: dict = dict(jitter(track.face_cx, track.face_cy))

    diff = track.frame_diff[1:]                    # 第 0 帧没有前一帧
    spikes = find_spikes(diff)
    freezes = find_freezes(diff)
    out["frame_diff_median"] = (
        round(float(np.nanmedian(diff)), 4) if diff.size else None)
    out["jump_count"] = len(spikes)
    out["jump_times_s"] = [round((i + 1) / fps, 2) for i in spikes[:20]]
    out["freeze_count"] = len(freezes)
    out["freeze_spans_s"] = [
        [round((s + 1) / fps, 2), round(n / fps, 2)] for s, n in freezes[:20]]

    # 曝光/色调漂移：整帧灰度均值在实测区间内的落差（不外推，理由同 identity）
    mean = track.frame_mean
    finite = mean[np.isfinite(mean)]
    if finite.size >= 4:
        out["luma_mean"] = round(float(finite.mean()), 2)
        out["luma_std"] = round(float(finite.std()), 3)
        slope = float(np.polyfit(np.arange(finite.size), finite, 1)[0])
        out["luma_drift_observed"] = round(slope * (finite.size - 1), 2)

    # 转头幅度：平台要求正视镜头，不要大幅偏头
    yaw = track.yaw_proxy
    fy = yaw[np.isfinite(yaw)]
    if fy.size >= 3:
        out["yaw_std"] = round(float(fy.std()), 4)
        out["yaw_abs_max"] = round(float(np.abs(fy - np.median(fy)).max()), 4)
    return out
