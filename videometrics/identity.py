#!/usr/bin/env python3
"""一致性：全程还是同一个人。

对训练素材来说这一维比「好看」更要命。平台是拿这段视频去学一个形象，
如果人物在 60 秒里慢慢漂成另一个人，学出来的就是一张平均脸——糊、不像本人，
而且这个损失在成片阶段是不可逆的。

两种测法互补：

  embedding 漂移   SFace 向量的余弦相似度。抓的是外观（肤色、五官、发型）。
  几何漂移         关键点比例。抓的是结构（脸变宽、五官间距变了）。

分别看首帧基线和滑动窗口：整体缓慢漂移和局部突变是两种不同的失败。
"""

from __future__ import annotations

import numpy as np


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return float("nan")
    return float(np.dot(a, b) / (na * nb))


def drift_slope(values: np.ndarray) -> float:
    """对序列做一次线性拟合，返回每帧的斜率。

    负斜率说明相似度在持续走低，也就是身份在单向漂移；
    如果只是抖动，斜率会接近 0 而方差很大。两者要区分开。
    """
    y = np.asarray(values, dtype=float)
    mask = np.isfinite(y)
    if mask.sum() < 3:
        return float("nan")
    x = np.arange(len(y))[mask]
    return float(np.polyfit(x, y[mask], 1)[0])


def identity_metrics(track, *, reference_embedding=None) -> dict:
    """身份一致性。

    注意所有「漂移」都只报**这段素材实测区间内**的变化量，不外推。
    10 秒片段的斜率外推到 60 秒会放大 6 倍噪声，得出「相似度下滑 0.76」
    这种不可能的数（余弦相似度总量程才 2）。要横向比不同长度的素材，
    用 duration_s 自己归一化。
    """
    fps = max(track.fps, 1e-6)
    out: dict = {"duration_s": round(track.frame_count / fps, 2)}

    embs = [e for e in (track.embeddings or []) if e is not None]
    if len(embs) >= 2:
        base = np.asarray(reference_embedding if reference_embedding is not None else embs[0])
        out["baseline"] = "reference_image" if reference_embedding is not None else "first_frame"
        sims = np.array([cosine_similarity(base, e) for e in embs])
        finite = sims[np.isfinite(sims)]
        if finite.size:
            out["embed_sim_mean"] = round(float(finite.mean()), 4)
            out["embed_sim_min"] = round(float(finite.min()), 4)
            out["embed_sim_p5"] = round(float(np.percentile(finite, 5)), 4)
            out["embed_sim_spread"] = round(float(finite.max() - finite.min()), 4)
            slope = drift_slope(sims)
            if np.isfinite(slope):
                # 拟合直线在实测区间两端之间的落差，负值表示单向漂移
                out["embed_drift_observed"] = round(slope * (len(sims) - 1), 4)
        # 相邻采样点相似度：突变说明有跳变/换人
        step = np.array([cosine_similarity(embs[i], embs[i + 1])
                         for i in range(len(embs) - 1)])
        step = step[np.isfinite(step)]
        if step.size:
            out["embed_step_min"] = round(float(step.min()), 4)
    else:
        out["note"] = "没有可用的人脸向量（缺 SFace 模型或没检出脸），只看几何漂移"

    # 几何：嘴宽/脸高的稳定性。归一化过，所以远近变化不影响。
    ratio = track.mouth_width
    finite = ratio[np.isfinite(ratio)]
    if finite.size >= 3:
        out["mouth_width_ratio_std"] = round(float(finite.std()), 4)
        slope = drift_slope(ratio)
        if np.isfinite(slope):
            out["mouth_width_drift_observed"] = round(slope * (len(ratio) - 1), 4)

    size = track.face_size
    finite_size = size[np.isfinite(size)]
    if finite_size.size >= 3:
        out["face_size_mean"] = round(float(finite_size.mean()), 4)
        out["face_size_std"] = round(float(finite_size.std()), 4)
    return out
