"""重叠区融合与颜色校正 —— 长视频时间一致性的"最后一公里"。

模型级锚定（previous_frames）已经保证了语义连续：新块的前 overlap 帧 latent
直接复用上一块的输出。但 VAE 编解码往返 + 采样噪声仍会引入：

1. 重叠区两侧**极轻微的像素差**（若直接硬切换，逐块累积后偶尔可察觉）；
2. 低频**颜色/亮度漂移**（每块偏一点点，几十块后明显偏色）。

对应两个工具：

- :func:`blend_overlap`：对重叠区做余弦（Hann）或高斯渐变权重的逐像素融合，
  前块淡出、后块淡入，把残余数值差抹平；
- :func:`reinhard_color_match`：Reinhard 风格的 LAB 均值/方差迁移，把新块的
  颜色统计对齐到上一块末帧，阻断漂移累积（与社区 scail-auto-extend 一致）。
"""

from __future__ import annotations

from typing import List, Sequence

import cv2
import numpy as np


def cosine_weights(n: int) -> np.ndarray:
    """余弦（Hann 半窗）渐变权重，长度 n，单调从 ~0 升到 ~1。

    w_k = (1 - cos(pi * (k+1) / (n+1))) / 2
    端点不取 0/1（避免与相邻非融合帧完全重复），中段平滑过渡。
    """
    if n <= 0:
        return np.zeros(0, dtype=np.float64)
    k = np.arange(1, n + 1, dtype=np.float64)
    return (1.0 - np.cos(np.pi * k / (n + 1))) / 2.0


def gaussian_weights(n: int, sigma: float = 0.35) -> np.ndarray:
    """高斯累积（erf 形）渐变权重，长度 n，单调从 ~0 升到 ~1。

    以区间中心为均值的高斯 CDF 采样，sigma 相对区间长度归一化。
    """
    if n <= 0:
        return np.zeros(0, dtype=np.float64)
    x = (np.arange(1, n + 1, dtype=np.float64) - (n + 1) / 2.0) / ((n + 1) * sigma)
    # 标准正态 CDF（用 erf 表达）
    from math import erf

    cdf = np.array([0.5 * (1.0 + erf(v / np.sqrt(2.0))) for v in x])
    return cdf


def make_weights(n: int, curve: str = "cosine") -> np.ndarray:
    if curve == "gaussian":
        return gaussian_weights(n)
    if curve == "cosine":
        return cosine_weights(n)
    raise ValueError(f"未知融合曲线：{curve!r}（可选 cosine / gaussian）")


def blend_overlap(
    prev_tail: Sequence[np.ndarray],
    next_head: Sequence[np.ndarray],
    curve: str = "cosine",
) -> List[np.ndarray]:
    """对重叠区逐帧加权融合。

    第 k 帧输出 = (1 - w_k) * 前块尾帧 + w_k * 后块头帧，w_k 由渐变曲线给出，
    随 k 增大后块权重增大 → 前块平滑淡出、后块平滑淡入。
    """
    n = min(len(prev_tail), len(next_head))
    if n == 0:
        return []
    weights = make_weights(n, curve)
    out: List[np.ndarray] = []
    for k in range(n):
        w = float(weights[k])
        a = prev_tail[k].astype(np.float32)
        b = next_head[k].astype(np.float32)
        out.append(np.clip((1.0 - w) * a + w * b, 0, 255).astype(np.uint8))
    return out


def reinhard_color_match(
    frames: Sequence[np.ndarray],
    reference: np.ndarray,
    strength: float = 1.0,
) -> List[np.ndarray]:
    """Reinhard-LAB 颜色迁移：把 frames 的颜色统计对齐到 reference。

    在 LAB 空间对每个通道做 (x - mean_src) / std_src * std_ref + mean_ref，
    只搬运全局颜色统计、不改变结构，用于阻断逐块颜色漂移的累积。

    Parameters
    ----------
    frames:
        待校正帧序列（BGR uint8）。统计量在整段上计算并统一应用，
        避免逐帧统计导致的闪烁。
    reference:
        参考帧（通常是上一块校正后的末帧，BGR uint8）。
    strength:
        校正强度 0~1，1 为完全对齐。
    """
    if not len(frames):
        return []
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength == 0.0:
        return [f.copy() for f in frames]

    ref_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref_mean = ref_lab.reshape(-1, 3).mean(axis=0)
    ref_std = ref_lab.reshape(-1, 3).std(axis=0) + 1e-6

    src_stack = np.stack(
        [cv2.cvtColor(f, cv2.COLOR_BGR2LAB).astype(np.float32) for f in frames]
    )
    src_mean = src_stack.reshape(-1, 3).mean(axis=0)
    src_std = src_stack.reshape(-1, 3).std(axis=0) + 1e-6

    out: List[np.ndarray] = []
    for lab in src_stack:
        matched = (lab - src_mean) / src_std * ref_std + ref_mean
        # 按 strength 在原始与完全匹配之间插值
        mixed = lab + strength * (matched - lab)
        mixed = np.clip(mixed, 0, 255).astype(np.uint8)
        out.append(cv2.cvtColor(mixed, cv2.COLOR_LAB2BGR))
    return out
