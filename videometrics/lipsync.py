#!/usr/bin/env python3
"""协调性：嘴在不在跟着声音动。

做法是把两条一维信号对齐——音频包络（每视频帧一个 RMS 值）和嘴部开口序列，
然后算互相关。这不是 SyncNet 那种学出来的判别器，但它有两个好处：
确定性（同一输入必得同一结果，可回归测试），以及可解释（超前/滞后多少毫秒
是个有物理意义的数）。

局限必须写在这里：**这个指标偏爱正对镜头、脸不动的说话人**。AGI-Eval 报告里
Gemini-Omni 口型分最高，恰恰因为它生成的脸几乎不动，对检测工具最友好。
所以 lipsync 分数高不等于视频好，要和 aliveness/stability 一起看。
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np


def audio_envelope(video, *, fps: float, sample_rate: int = 16000) -> np.ndarray:
    """把音轨解成每视频帧一个 RMS 值。没有音轨返回空数组。"""
    if not shutil.which("ffmpeg"):
        return np.zeros(0)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"],
        capture_output=True,
    )
    if out.returncode != 0 or not out.stdout:
        return np.zeros(0)

    pcm = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    per_frame = max(int(round(sample_rate / max(fps, 1e-6))), 1)
    usable = (len(pcm) // per_frame) * per_frame
    if usable == 0:
        return np.zeros(0)
    return np.sqrt((pcm[:usable].reshape(-1, per_frame) ** 2).mean(axis=1))


def _zscore(x: np.ndarray) -> np.ndarray:
    """去均值除标准差，并把 NaN 用序列均值补上。常数序列返回全 0。"""
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    finite = np.isfinite(x)
    if not finite.any():
        return np.zeros_like(x)
    filled = np.where(finite, x, x[finite].mean())
    std = filled.std()
    if std < 1e-9:
        return np.zeros_like(filled)
    return (filled - filled.mean()) / std


def align_lag(signal: np.ndarray, reference: np.ndarray,
              max_lag: int) -> tuple[int, float, float]:
    """在 ±max_lag 帧内找让两条信号相关最高的偏移。

    返回 (最佳偏移帧数, 该偏移处的相关系数, 零偏移处的相关系数)。
    偏移为正表示 signal 落后于 reference（嘴比声音慢）。
    """
    a = _zscore(signal)
    b = _zscore(reference)
    n = min(len(a), len(b))
    if n < 4:
        return 0, float("nan"), float("nan")
    a, b = a[:n], b[:n]
    max_lag = int(min(max_lag, n - 2))

    best_lag, best_corr = 0, -2.0
    zero_corr = float("nan")
    # 按 |lag| 从小到大遍历，配合严格大于的比较，让相关度打平时取更接近 0 的偏移。
    # 否则信号退化（常数、纯噪声）时相关度处处相等，返回的会是搜索区间的端点，
    # 看起来像「稳定超前 400ms」，其实只是遍历顺序的产物。
    lags = sorted(range(-max_lag, max_lag + 1), key=lambda k: (abs(k), k))
    for lag in lags:
        if lag >= 0:
            x, y = a[lag:], b[:n - lag]
        else:
            x, y = a[:n + lag], b[-lag:]
        if len(x) < 4 or x.std() < 1e-9 or y.std() < 1e-9:
            corr = 0.0
        else:
            corr = float(np.corrcoef(x, y)[0, 1])
        if lag == 0:
            zero_corr = corr
        if corr > best_corr + 1e-12:
            best_lag, best_corr = lag, corr
    return best_lag, best_corr, zero_corr


def onset_index(signal: np.ndarray, *, threshold_ratio: float = 0.25,
                min_run: int = 2) -> int:
    """信号第一次「明显起来」的下标。找不到返回 -1。

    阈值取该序列自身动态范围的一个比例，而不是绝对值，因为不同模型的嘴部
    幅度基线差很多。要求连续 min_run 帧超阈，避免单帧噪声。
    """
    x = np.asarray(signal, dtype=float)
    finite = np.isfinite(x)
    if finite.sum() < min_run + 1:
        return -1
    vals = x[finite]
    lo, hi = np.percentile(vals, 5), np.percentile(vals, 95)
    if hi - lo < 1e-9:
        return -1
    level = lo + (hi - lo) * threshold_ratio

    run = 0
    for i, v in enumerate(x):
        if np.isfinite(v) and v >= level:
            run += 1
            if run >= min_run:
                return i - min_run + 1
        else:
            run = 0
    return -1


# 相关性低于这个值时，互相关的峰位基本是噪声，报出来的「超前/滞后 xx ms」没有意义
LAG_MIN_CORR = 0.25


def detect_silence_head(envelope: np.ndarray, *, fps: float,
                        quiet_ratio: float = 0.05) -> float:
    """从音频包络本身量出开头有多少秒是静的。

    比相信调用方传进来的 silence_seconds 可靠：素材可能被重新剪过、
    或者根本没加静默头。阈值取全片 RMS 中位数的一个比例，
    而不是绝对 dB，这样对整体音量大小不敏感。
    """
    env = np.asarray(envelope, dtype=float)
    if env.size == 0:
        return 0.0
    active = env[env > 0]
    if active.size == 0:
        return float(env.size / max(fps, 1e-6))
    level = float(np.median(active)) * quiet_ratio
    quiet = 0
    for v in env:
        if v <= level:
            quiet += 1
        else:
            break
    return round(quiet / max(fps, 1e-6), 2)


def lipsync_metrics(track, envelope: np.ndarray, *,
                    silence_seconds: float = 0.0) -> dict:
    """协调性指标。

    silence_seconds 只作参考，实际用的是从音频里量出来的静默长度——
    传进来的值可能和素材不符（比如这条根本没加静默头）。
    """
    fps = max(track.fps, 1e-6)
    mouth = track.mouth_open
    jaw = track.jaw_open
    n = min(len(mouth), len(envelope))

    out: dict = {
        "frames_scored": int(n),
        "duration_s": round(track.frame_count / fps, 2),
        "has_audio": bool(len(envelope) > 0),
    }
    if n < 8:
        out["note"] = "帧数或音频不足，口型同步无法评估"
        return out

    max_lag = int(round(fps * 0.4))          # ±400ms，超过这个量级人已经明显觉得不同步
    lag, corr, corr0 = align_lag(mouth[:n], envelope[:n], max_lag)
    out["corr_best"] = round(corr, 4) if np.isfinite(corr) else None
    out["corr_zero_lag"] = round(corr0, 4) if np.isfinite(corr0) else None

    # 只有相关性够高、且峰值没顶在搜索边界上时，偏移量才有解释意义
    reliable = (np.isfinite(corr) and corr >= LAG_MIN_CORR
                and abs(lag) < max_lag)
    out["lag_reliable"] = bool(reliable)
    if reliable:
        out["lag_frames"] = lag
        out["lag_ms"] = round(lag * 1000.0 / fps, 1)
    else:
        reason = ("相关性过低" if not np.isfinite(corr) or corr < LAG_MIN_CORR
                  else "峰值顶在 ±400ms 搜索边界")
        out["lag_note"] = f"偏移量不可信（{reason}），不作判定"

    jaw_lag, jaw_corr, _ = align_lag(jaw[:n], envelope[:n], max_lag)
    out["jaw_corr_best"] = round(jaw_corr, 4) if np.isfinite(jaw_corr) else None
    if np.isfinite(jaw_corr) and jaw_corr >= LAG_MIN_CORR and abs(jaw_lag) < max_lag:
        out["jaw_lag_ms"] = round(jaw_lag * 1000.0 / fps, 1)

    # 张嘴幅度的动态范围。太小说明模型在「小声嘟囔」，训出来的形象会欠张口。
    finite_mouth = mouth[np.isfinite(mouth)]
    if finite_mouth.size:
        p5, p95 = np.percentile(finite_mouth, [5, 95])
        out["mouth_range"] = round(float(p95 - p5), 4)
        out["mouth_std"] = round(float(finite_mouth.std()), 4)

    measured = detect_silence_head(envelope[:n], fps=fps)
    out["silence_measured_s"] = measured
    if silence_seconds > 0:
        out["silence_declared_s"] = round(silence_seconds, 2)
        if abs(measured - silence_seconds) > 0.4:
            out["silence_mismatch"] = True

    # 开口提前量：嘴先动还是声先响。只在音频确实有静默头时才有意义。
    mouth_onset = onset_index(mouth[:n])
    audio_onset = onset_index(envelope[:n])
    if mouth_onset >= 0:
        out["mouth_onset_s"] = round(mouth_onset / fps, 2)
    if mouth_onset >= 0 and audio_onset >= 0:
        out["onset_offset_ms"] = round((mouth_onset - audio_onset) * 1000.0 / fps, 1)
    return out
