#!/usr/bin/env python3
"""把五个维度的指标汇总成一份可比对的报告。

刻意**不输出总分**。理由：把十几个量纲不同的指标加权成一个数，权重是拍脑袋
定的，那还是主观评价，只是把主观藏进了代码里。真正的做法是先攒够你的主观
判断（对照页上的成对选择），再用这些标注去拟合/验证权重——
MultiRef-Compass 就是这么做的，它报告自己和人类偏好的皮尔逊相关是 0.90~0.96。
在拿到标注之前，这里只给三样东西：

  metrics    五个维度的原始指标向量，供后续拟合
  defects    带时间戳和严重度的缺陷清单，可以直接跳到那一秒去看
  segments   每 2 秒一行的时间线，用来定位问题出在开头还是结尾

缺陷判定用的阈值都写在 THRESHOLDS 里，集中一处方便讨论和调整。
每条阈值后面都注明了依据，凭感觉定的会写「待校准」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .aliveness import BLINK_RATE_NATURAL, aliveness_metrics
from .facetrack import MetricsUnavailable, track_faces
from .identity import identity_metrics
from .lipsync import audio_envelope, lipsync_metrics
from .plausibility import plausibility_metrics
from .stability import stability_metrics

SEGMENT_SECONDS = 2.0

THRESHOLDS = {
    # 口型-音频相关性。0.3 以下基本可以认为嘴没跟着声音走。**待校准**。
    "lipsync_corr_major": 0.15,
    "lipsync_corr_minor": 0.30,
    # 音画偏移。±150ms 是行业里常引用的可感知阈值，这里取整到 120/200。
    "lag_ms_major": 200.0,
    "lag_ms_minor": 120.0,
    # 静默段嘴动幅度 / 口播段嘴动幅度。**待校准**。
    "silence_mouth_ratio_major": 0.6,
    "silence_mouth_ratio_minor": 0.35,
    # 静默段最大张颌幅度，超过就是明显的吸气预备动作。**待校准**。
    "silence_jaw_major": 0.12,
    "silence_jaw_minor": 0.06,
    # 身份漂移：实测区间内余弦相似度的单向跌幅。**待校准**。
    "identity_drift_major": 0.08,
    "identity_drift_minor": 0.04,
    # 脸检测率。低于此值说明有大段畸变或出画。
    "detect_rate_major": 0.90,
    "detect_rate_minor": 0.98,
    # 实测区间内的灰度均值漂移（0~255 量程）。**待校准**。
    "luma_drift_major": 12.0,
    "luma_drift_minor": 6.0,
}


@dataclass
class Defect:
    dimension: str          # 合理性 / 协调性 / 稳定性 / 一致性 / 活性
    code: str
    severity: str           # major / minor
    detail: str
    at_seconds: float | None = None

    def to_dict(self) -> dict:
        return {"dimension": self.dimension, "code": self.code,
                "severity": self.severity, "detail": self.detail,
                "at_seconds": self.at_seconds}


@dataclass
class Evaluation:
    available: bool
    metrics: dict = field(default_factory=dict)
    defects: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    note: str = ""

    @property
    def major_count(self) -> int:
        return sum(1 for d in self.defects if d.severity == "major")

    @property
    def minor_count(self) -> int:
        return sum(1 for d in self.defects if d.severity == "minor")

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "note": self.note,
            "major_count": self.major_count,
            "minor_count": self.minor_count,
            "metrics": self.metrics,
            "defects": [d.to_dict() for d in self.defects],
            "segments": self.segments,
        }

    def render(self) -> str:
        if not self.available:
            return f"客观指标不可用：{self.note}"
        lines = [f"缺陷 {self.major_count} 重 / {self.minor_count} 轻"]
        for dim, block in self.metrics.items():
            items = ", ".join(f"{k}={v}" for k, v in block.items()
                              if isinstance(v, (int, float, bool)))
            lines.append(f"  [{dim}] {items}")
        for d in self.defects:
            at = f" @{d.at_seconds:g}s" if d.at_seconds is not None else ""
            lines.append(f"  ({d.severity}) {d.dimension}/{d.code}{at}: {d.detail}")
        return "\n".join(lines)


def _segments(track, envelope: np.ndarray) -> list[dict]:
    """每 2 秒一行的时间线。参考「30 秒分段标注」的做法：不先合成总分，
    而是保留问题发生的时间，方便复看和单变量复测。"""
    fps = max(track.fps, 1e-6)
    step = max(int(round(SEGMENT_SECONDS * fps)), 1)
    rows = []
    for start in range(0, track.frame_count, step):
        end = min(start + step, track.frame_count)
        if end - start < 2:
            break
        sl = slice(start, end)
        mouth = track.mouth_open[sl]
        env = envelope[sl] if len(envelope) >= end else np.zeros(0)
        rows.append({
            "t0": round(start / fps, 1),
            "t1": round(end / fps, 1),
            "detect_rate": round(float(track.detected[sl].mean()), 3),
            "mouth_std": (round(float(np.nanstd(mouth)), 4)
                          if np.isfinite(mouth).any() else None),
            "audio_rms": round(float(env.mean()), 5) if env.size else None,
            "blink": int((track.blink[sl] >= 0.5).any()) if np.isfinite(track.blink[sl]).any() else 0,
            "frame_diff": (round(float(np.nanmean(track.frame_diff[sl])), 3)
                           if np.isfinite(track.frame_diff[sl]).any() else None),
        })
    return rows


def _collect_defects(metrics: dict, *, silence_seconds: float) -> list[Defect]:
    t = THRESHOLDS
    out: list[Defect] = []
    lip = metrics.get("协调性", {})
    alive = metrics.get("活性", {})
    ident = metrics.get("一致性", {})
    stab = metrics.get("稳定性", {})
    plaus = metrics.get("合理性", {})

    corr = lip.get("corr_best")
    if corr is not None:
        if corr < t["lipsync_corr_major"]:
            out.append(Defect("协调性", "lipsync_weak", "major",
                              f"口型与音频相关性只有 {corr:.2f}，基本没跟着声音动"))
        elif corr < t["lipsync_corr_minor"]:
            out.append(Defect("协调性", "lipsync_weak", "minor",
                              f"口型与音频相关性 {corr:.2f}，偏低"))

    lag = lip.get("lag_ms")
    if lag is not None:
        if abs(lag) >= t["lag_ms_major"]:
            out.append(Defect("协调性", "lipsync_lag", "major",
                              f"口型比音频{'滞后' if lag > 0 else '超前'} {abs(lag):.0f}ms"))
        elif abs(lag) >= t["lag_ms_minor"]:
            out.append(Defect("协调性", "lipsync_lag", "minor",
                              f"口型偏移 {lag:+.0f}ms"))

    if lip.get("silence_mismatch"):
        out.append(Defect("协调性", "silence_mismatch", "minor",
                          f"声称加了 {lip.get('silence_declared_s')}s 静默头，"
                          f"实测只有 {lip.get('silence_measured_s')}s"))

    # 静默段的判定一律用实测值，传进来的 silence_seconds 可能和素材不符
    measured_silence = lip.get("silence_measured_s") or 0.0
    if measured_silence >= 0.5:
        ratio = alive.get("silence_vs_speech_mouth_ratio")
        if ratio is not None:
            if ratio >= t["silence_mouth_ratio_major"]:
                out.append(Defect("活性", "silence_mouth_active", "major",
                                  f"静默段嘴动幅度达口播段的 {ratio:.0%}，没有真正闭口"))
            elif ratio >= t["silence_mouth_ratio_minor"]:
                out.append(Defect("活性", "silence_mouth_active", "minor",
                                  f"静默段嘴动幅度为口播段的 {ratio:.0%}"))
        open_ratio = alive.get("silence_open_vs_speech_ratio")
        if open_ratio is not None and open_ratio >= 0.85:
            out.append(Defect("活性", "silence_mouth_not_closed", "major",
                              f"静默段最大开口达口播段的 {open_ratio:.0%}，嘴没闭上"))
        jaw = alive.get("silence_jaw_max")
        if jaw is not None:
            if jaw >= t["silence_jaw_major"]:
                out.append(Defect("活性", "inhale_before_speech", "major",
                                  f"静默段张颌幅度到 {jaw:.2f}，像深吸一口气"))
            elif jaw >= t["silence_jaw_minor"]:
                out.append(Defect("活性", "inhale_before_speech", "minor",
                                  f"静默段张颌幅度 {jaw:.2f}，有张嘴预备的迹象"))
        if alive.get("silence_frozen"):
            out.append(Defect("活性", "silence_frozen", "major",
                              "静默段画面几乎不动，像在放静止帧"))
        if alive.get("silence_blink_count") == 0 and measured_silence >= 1.5:
            out.append(Defect("活性", "no_blink_in_silence", "minor",
                              f"{measured_silence:g}s 静默里一次没眨眼"))

    blinks = alive.get("blink_count")
    if blinks == 0:
        out.append(Defect("活性", "no_blink", "major", "全片一次没眨眼"))
    elif alive.get("blink_rate_natural") is False:
        rate = alive.get("blink_rate_per_min")
        lo, hi = BLINK_RATE_NATURAL
        out.append(Defect("活性", "blink_rate_odd", "minor",
                          f"眨眼 {rate}/分钟，偏离自然区间 {lo:g}~{hi:g}"))

    drift = ident.get("embed_drift_observed")
    if drift is not None and drift < 0:
        magnitude = abs(drift)
        span = ident.get("duration_s")
        window = f"（{span:g}s 区间内）" if span else ""
        if magnitude >= t["identity_drift_major"]:
            out.append(Defect("一致性", "identity_drift", "major",
                              f"身份相似度单向下滑 {magnitude:.3f}{window}"))
        elif magnitude >= t["identity_drift_minor"]:
            out.append(Defect("一致性", "identity_drift", "minor",
                              f"身份相似度单向下滑 {magnitude:.3f}{window}"))

    for start, _length in (stab.get("freeze_spans_s") or [])[:5]:
        out.append(Defect("稳定性", "freeze", "minor", "画面冻结", at_seconds=start))
    for at in (stab.get("jump_times_s") or [])[:5]:
        out.append(Defect("稳定性", "frame_jump", "major", "疑似跳帧", at_seconds=at))

    luma = stab.get("luma_drift_observed")
    if luma is not None:
        if abs(luma) >= t["luma_drift_major"]:
            out.append(Defect("稳定性", "luma_drift", "major",
                              f"亮度漂移 {luma:+.1f}（0~255 量程）"))
        elif abs(luma) >= t["luma_drift_minor"]:
            out.append(Defect("稳定性", "luma_drift", "minor",
                              f"亮度漂移 {luma:+.1f}（0~255 量程）"))

    rate = plaus.get("face_detect_rate")
    if rate is not None:
        if rate < t["detect_rate_major"]:
            out.append(Defect("合理性", "face_undetected", "major",
                              f"只有 {rate:.0%} 的帧能检出人脸"))
        elif rate < t["detect_rate_minor"]:
            out.append(Defect("合理性", "face_undetected", "minor",
                              f"{(1 - rate):.1%} 的帧检不出人脸"))
    return out


def evaluate(video, *, silence_seconds: float = 0.0,
             reference_image=None, max_frames: int = 3000) -> Evaluation:
    """跑完整评测。缺依赖时返回 available=False 而不是抛异常。"""
    try:
        track = track_faces(video, max_frames=max_frames)
    except MetricsUnavailable as exc:
        return Evaluation(available=False, note=str(exc))

    if track.frame_count == 0:
        return Evaluation(available=False, note="视频里读不到帧")

    envelope = audio_envelope(video, fps=track.fps)
    reference_embedding = _reference_embedding(reference_image) if reference_image else None

    lip = lipsync_metrics(track, envelope, silence_seconds=silence_seconds)
    # 静默段相关的指标一律按**实测**长度算。传进来的 silence_seconds 只用于
    # 交叉核对：素材可能被重剪过，或者压根没加静默头。
    measured_silence = lip.get("silence_measured_s") or 0.0

    metrics = {
        "协调性": lip,
        "一致性": identity_metrics(track, reference_embedding=reference_embedding),
        "稳定性": stability_metrics(track),
        "合理性": plausibility_metrics(track),
        "活性": aliveness_metrics(track, silence_seconds=measured_silence),
    }
    return Evaluation(
        available=True,
        metrics=metrics,
        defects=_collect_defects(metrics, silence_seconds=measured_silence),
        segments=_segments(track, envelope),
    )


def evaluate_to_dict(video, **kwargs) -> dict:
    return evaluate(video, **kwargs).to_dict()


def _reference_embedding(image):
    """从角色图取一个基线向量，用来判断成片像不像**你给的那张图**，
    而不只是「自己跟自己一致」。"""
    try:
        import cv2
    except ImportError:
        return None
    from . import models
    from .facetrack import _embed, _make_recognizer

    frame = cv2.imread(str(image))
    if frame is None:
        return None
    recognizer = _make_recognizer(cv2)
    if recognizer is None:
        return None
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
    except ImportError:
        return None
    path = models.ensure(models.FACE_LANDMARKER)
    if path is None:
        return None
    options = vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(path)),
        running_mode=vision.RunningMode.IMAGE, num_faces=1)
    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = landmarker.detect(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if not result.face_landmarks:
        return None
    h, w = frame.shape[:2]
    return _embed(cv2, recognizer, frame, result.face_landmarks[0], w, h)
