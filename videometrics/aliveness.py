#!/usr/bin/env python3
"""活性：静默段是不是活人。

这一维不在 AGI-Eval 的四维里，是我们这个项目特有的。素材要求开头静默闭口
1~3 秒，而这一段最容易出两种毛病：

  冻住   画面近乎静止、不眨眼，像在放照片（用参考图静止帧顶开头就是这个后果）
  吸气   模型把静音演成「深吸一口气」，张嘴、耸肩、下颌先动

两者都会被平台学进形象里。眨眼是最好的活体信号：成年人自然眨眼频率大约
每分钟 15~20 次，也就是 10 秒里 2~3 次。一次都不眨基本可以判定冻住。
"""

from __future__ import annotations

import numpy as np

# 眨眼 blendshape 超过这个值算「眼睑闭合中」。MediaPipe 的 eyeBlink 在完全闭眼时
# 接近 1，睁眼时通常低于 0.2，0.5 是留了余量的分界。
BLINK_THRESHOLD = 0.5
# 成年人静息眨眼频率的参考区间，次/分钟
BLINK_RATE_NATURAL = (10.0, 30.0)
# 短片段折算不出可信的每分钟频率：10 秒里多一次眨眼就等于每分钟差 6 次，
# 所以时长不够时只报「一次都没眨」这种确定性的结论，不判频率是否自然。
BLINK_RATE_MIN_SECONDS = 20.0


def count_blinks(blink: np.ndarray, *, threshold: float = BLINK_THRESHOLD,
                 min_gap: int = 2) -> int:
    """数眨眼次数：统计信号「上穿阈值」的次数，而不是超阈的帧数。

    min_gap 是两次眨眼之间至少要间隔的帧数，用来把一次眨眼过程中的抖动合成一次。
    """
    x = np.asarray(blink, dtype=float)
    above = np.isfinite(x) & (x >= threshold)
    count, last = 0, -(10 ** 9)
    for i, flag in enumerate(above):
        if flag and not above[i - 1] if i else flag:
            if i - last >= min_gap:
                count += 1
                last = i
    return count


def aliveness_metrics(track, *, silence_seconds: float = 0.0) -> dict:
    fps = max(track.fps, 1e-6)
    duration = track.frame_count / fps
    out: dict = {"duration_s": round(duration, 2)}

    blinks = count_blinks(track.blink)
    out["blink_count"] = blinks
    if duration > 0:
        out["blink_rate_per_min"] = round(blinks * 60.0 / duration, 1)
        if duration >= BLINK_RATE_MIN_SECONDS:
            lo, hi = BLINK_RATE_NATURAL
            out["blink_rate_natural"] = bool(lo <= out["blink_rate_per_min"] <= hi)
        else:
            out["blink_rate_note"] = (
                f"只有 {duration:.1f}s，折算每分钟频率误差太大，不判是否自然")

    if silence_seconds <= 0:
        return out

    head = int(min(round(silence_seconds * fps), track.frame_count))
    if head < 3:
        return out

    out["silence_frames"] = head
    out["silence_blink_count"] = count_blinks(track.blink[:head])

    # 冻住检测：静默段的画面差分。近零说明连续帧几乎一样。
    head_diff = track.frame_diff[1:head]
    if head_diff.size:
        out["silence_frame_diff_mean"] = round(float(np.nanmean(head_diff)), 4)
        out["silence_frozen"] = bool(np.nanmean(head_diff) < 0.15)

    # 吸气检测：静默段本该闭嘴，嘴/下颌却动了
    head_mouth = track.mouth_open[:head]
    head_jaw = track.jaw_open[:head]
    if np.isfinite(head_mouth).any():
        out["silence_mouth_max"] = round(float(np.nanmax(head_mouth)), 4)
        out["silence_mouth_std"] = round(float(np.nanstd(head_mouth)), 4)
    if np.isfinite(head_jaw).any():
        out["silence_jaw_max"] = round(float(np.nanmax(head_jaw)), 4)

    # 和口播段对比，两个角度都要看：
    #   动没动  标准差之比。嘴在静默段乱动 = 提前说话/吸气。
    #   闭没闭  最大开口 / 口播段 p95 开口。嘴被张着一动不动时标准差很小，
    #           只看标准差会漏判，但这种「张着嘴等」同样会被平台学进去。
    tail_mouth = track.mouth_open[head:]
    if np.isfinite(head_mouth).any() and np.isfinite(tail_mouth).any():
        speak_std = float(np.nanstd(tail_mouth))
        silent_std = float(np.nanstd(head_mouth))
        out["silence_vs_speech_mouth_ratio"] = (
            round(silent_std / speak_std, 3) if speak_std > 1e-9 else None)

        speak_open = float(np.nanpercentile(tail_mouth, 95))
        silent_open = float(np.nanmax(head_mouth))
        out["silence_open_vs_speech_ratio"] = (
            round(silent_open / speak_open, 3) if speak_open > 1e-9 else None)
    return out
