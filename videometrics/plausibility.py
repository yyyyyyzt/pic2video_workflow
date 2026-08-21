#!/usr/bin/env python3
"""合理性：画面本身有没有崩。

AGI-Eval 报告里这一维的高频缺陷是「牙齿形态畸变」「手部形变」「背景文字乱码」
「光影不合理」。其中能用便宜手段客观量化的是：

  牙齿/口腔糊    嘴部区域的拉普拉斯方差。说话时口腔张开、牙齿露出，
                 清晰的牙齿边缘会让方差明显高；涂抹感强时方差塌下去。
                 关键是要和张嘴幅度一起看——闭着嘴当然不清晰。
  脸检测失败     整帧找不到脸，通常意味着严重畸变或人物出画。
  五官几何异常   归一化后的嘴宽/脸高等比例出现离群值。

手部形变和背景乱码需要检测/OCR 模型，暂不做，留给人眼；对照页会抽帧。
"""

from __future__ import annotations

import numpy as np


def longest_false_run(flags: np.ndarray) -> int:
    """最长连续 False 段长度。用来区分「偶发漏检」和「持续检不到」。"""
    best = run = 0
    for f in np.asarray(flags, dtype=bool):
        run = 0 if f else run + 1
        best = max(best, run)
    return best


def outlier_fraction(values: np.ndarray, *, k: float = 3.0) -> float:
    """离群帧占比，用中位数绝对偏差（MAD）判定，抗离群点本身的影响。

    MAD 为 0 是个需要单独处理的情况：多数帧取值完全相同时（生成视频里
    人物长时间纹丝不动就会这样），任何比例阈值都会被 0 吃掉，
    结果把明显的离群点判成「没有离群」。此时退化成「与中位数不等即离群」。
    """
    x = np.asarray(values, dtype=float)
    finite = x[np.isfinite(x)]
    if finite.size < 8:
        return float("nan")
    med = np.median(finite)
    deviation = np.abs(finite - med)
    mad = np.median(deviation)
    if mad < 1e-9:
        return float((deviation > 1e-9).mean())
    return float((deviation > k * 1.4826 * mad).mean())


def plausibility_metrics(track) -> dict:
    fps = max(track.fps, 1e-6)
    out: dict = {
        "face_detect_rate": round(track.detect_rate, 4),
        "face_miss_longest_s": round(longest_false_run(track.detected) / fps, 2),
    }

    # 牙齿清晰度只在「张着嘴」的帧上统计，否则闭嘴帧会把均值拉低
    mouth = track.mouth_open
    sharp = track.mouth_sharpness
    valid = np.isfinite(mouth) & np.isfinite(sharp)
    if valid.sum() >= 8:
        open_level = np.nanpercentile(mouth[valid], 60)
        speaking = valid & (mouth >= open_level)
        if speaking.sum() >= 4:
            vals = sharp[speaking]
            out["mouth_sharpness_open_mean"] = round(float(vals.mean()), 2)
            out["mouth_sharpness_open_p10"] = round(float(np.percentile(vals, 10)), 2)
        out["mouth_sharpness_all_mean"] = round(float(sharp[valid].mean()), 2)

    frac = outlier_fraction(track.mouth_width)
    if np.isfinite(frac):
        out["mouth_width_outlier_frac"] = round(frac, 4)
    frac_size = outlier_fraction(track.face_size)
    if np.isfinite(frac_size):
        out["face_size_outlier_frac"] = round(frac_size, 4)
    return out
