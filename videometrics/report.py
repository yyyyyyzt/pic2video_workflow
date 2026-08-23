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
from .anatomy import anatomy_metrics
from .facetrack import MetricsUnavailable, track_faces
from .identity import identity_metrics
from .lipsync import audio_envelope, lipsync_metrics
from .plausibility import plausibility_metrics
from .stability import stability_metrics

SEGMENT_SECONDS = 2.0

# 严重度分三级。哪一项归到哪一级，依据是使用方的实拍反馈，不是技术直觉：
#
#   major  会被平台学进形象、且事后无法修复的（体态畸变、身份漂移、跳帧）
#   minor  影响观感但不致命，或能靠提示词/重跑缓解的（口型偏移、亮度漂移）
#   info   只做记录，不参与排序。静默段的画面就属于这一档——
#          已确认「静默不参与训练，只是用来链接分割不同的动作」，
#          所以静默段是不是活人、有没有吸气，都不该影响方案取舍。
#          仍然测量，因为动作会渗进口播段，而且换平台档位时可能重新变重要。
SEVERITY_ORDER = ("major", "minor", "info")

THRESHOLDS = {
    # 口型-音频相关性。已确认「口型重要但不是决定性，观众不盯着嘴看」，
    # 所以整体降一级：原来的 major 阈值降为 minor，只有几乎完全不同步才算 major。
    "lipsync_corr_major": 0.08,
    "lipsync_corr_minor": 0.25,
    # 音画偏移。±150ms 是常引用的可感知阈值。同样降级为 minor 起。
    "lag_ms_major": 300.0,
    "lag_ms_minor": 150.0,
    # 静默段的三项都只记录，不定级（见上面的 info 说明），阈值只用来决定要不要记
    "silence_mouth_ratio_note": 0.5,
    "silence_jaw_note": 0.10,
    # 身份漂移：实测区间内余弦相似度的单向跌幅。训出平均脸不可逆，保持 major。
    "identity_drift_major": 0.08,
    "identity_drift_minor": 0.04,
    # 脸检测率。低于此值说明有大段畸变或出画。
    "detect_rate_major": 0.90,
    "detect_rate_minor": 0.98,
    # 实测区间内的灰度均值漂移（0~255 量程）。**待校准**。
    "luma_drift_major": 12.0,
    "luma_drift_minor": 6.0,
    # 体态：肩线倾角的波动范围（度）。整体协调是首要关注点，所以定得比别的严。**待校准**。
    "shoulder_tilt_range_major": 12.0,
    "shoulder_tilt_range_minor": 6.0,
    # 肩宽相对波动。身体被改形状。**待校准**。
    "shoulder_width_cv_major": 0.10,
    "shoulder_width_cv_minor": 0.05,
    # 手抬到肩以上的帧占比。平台要求手不遮挡面部和颈部。
    "hand_above_shoulder_major": 0.15,
    "hand_above_shoulder_minor": 0.03,
}

# 脸小于这个像素高度时，牙齿清晰度和眨眼这类细节指标不可信，只记不判。
# 384×576 的输出脸高大概 150~250px，正好落在边界上，所以这条必须有。
FACE_PX_DETAIL_FLOOR = 220.0


@dataclass
class Defect:
    dimension: str          # 体态 / 合理性 / 协调性 / 稳定性 / 一致性 / 活性
    code: str
    severity: str           # major / minor / info
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

    @property
    def info_count(self) -> int:
        return sum(1 for d in self.defects if d.severity == "info")

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "note": self.note,
            "major_count": self.major_count,
            "minor_count": self.minor_count,
            "info_count": self.info_count,
            "metrics": self.metrics,
            "defects": [d.to_dict() for d in self.defects],
            "segments": self.segments,
        }

    def render(self) -> str:
        if not self.available:
            return f"客观指标不可用：{self.note}"
        lines = [f"缺陷 {self.major_count} 重 / {self.minor_count} 轻"
                 + (f" / {self.info_count} 记录" if self.info_count else "")]
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


def _collect_defects(metrics: dict, *, silence_seconds: float,
                     face_px: float = float("nan")) -> list[Defect]:
    t = THRESHOLDS
    out: list[Defect] = []
    lip = metrics.get("协调性", {})
    alive = metrics.get("活性", {})
    ident = metrics.get("一致性", {})
    stab = metrics.get("稳定性", {})
    plaus = metrics.get("合理性", {})
    body = metrics.get("体态", {})

    detail_reliable = not np.isfinite(face_px) or face_px >= FACE_PX_DETAIL_FLOOR

    # --- 体态：整体协调是首要关注点 ---
    tilt = body.get("shoulder_tilt_range")
    if tilt is not None:
        if tilt >= t["shoulder_tilt_range_major"]:
            out.append(Defect("体态", "shoulder_unstable", "major",
                              f"肩线倾角波动 {tilt:.1f}°，身体在变形"))
        elif tilt >= t["shoulder_tilt_range_minor"]:
            out.append(Defect("体态", "shoulder_unstable", "minor",
                              f"肩线倾角波动 {tilt:.1f}°"))

    cv = body.get("shoulder_width_cv")
    if cv is not None:
        if cv >= t["shoulder_width_cv_major"]:
            out.append(Defect("体态", "body_reshaped", "major",
                              f"肩宽相对波动 {cv:.1%}，体型不稳定"))
        elif cv >= t["shoulder_width_cv_minor"]:
            out.append(Defect("体态", "body_reshaped", "minor",
                              f"肩宽相对波动 {cv:.1%}"))

    # 颈部用「时间维隆起」判定而不是绝对比值：绝对值会被分辨率和肤色平移，
    # 而「某几秒突然长出结构」在同一条视频内部是可比的
    rise = body.get("neck_edge_rise")
    if rise is not None:
        from .anatomy import NECK_EDGE_RISE_MAJOR, NECK_EDGE_RISE_MINOR
        absolute = body.get("neck_edge_ratio_p90")
        suffix = f"（峰值为脸颊的 {absolute:.2f} 倍）" if absolute else ""
        if rise >= NECK_EDGE_RISE_MAJOR:
            out.append(Defect("体态", "neck_bone_artifact", "major",
                              f"颈部纹理在片中隆起到中位数的 {rise:.1f} 倍，"
                              f"锁骨/颈筋被画出来了{suffix}",
                              at_seconds=body.get("neck_edge_worst_at_s")))
        elif rise >= NECK_EDGE_RISE_MINOR:
            out.append(Defect("体态", "neck_bone_artifact", "minor",
                              f"颈部纹理隆起到中位数的 {rise:.1f} 倍{suffix}",
                              at_seconds=body.get("neck_edge_worst_at_s")))

    hands = body.get("hand_above_shoulder_frac")
    if hands is not None:
        if hands >= t["hand_above_shoulder_major"]:
            out.append(Defect("体态", "hand_too_high", "major",
                              f"{hands:.0%} 的帧手抬到肩以上，可能遮挡面部"))
        elif hands >= t["hand_above_shoulder_minor"]:
            out.append(Defect("体态", "hand_too_high", "minor",
                              f"{hands:.0%} 的帧手抬到肩以上"))

    # --- 协调性：口型重要但不是决定性，整体降一级 ---
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

    # --- 活性：静默段只记录，不参与排序 ---
    # 依据：已确认静默段不参与平台训练，只用来链接分割不同的动作。
    # 所以这里全部降为 info，避免「静默段嘴没闭」这类项把方案排名带偏。
    measured_silence = lip.get("silence_measured_s") or 0.0
    if measured_silence >= 0.5:
        ratio = alive.get("silence_vs_speech_mouth_ratio")
        if ratio is not None and ratio >= t["silence_mouth_ratio_note"]:
            out.append(Defect("活性", "silence_mouth_active", "info",
                              f"静默段嘴动幅度达口播段的 {ratio:.0%}（不参与训练，仅记录）"))
        open_ratio = alive.get("silence_open_vs_speech_ratio")
        if open_ratio is not None and open_ratio >= 0.85:
            out.append(Defect("活性", "silence_mouth_not_closed", "info",
                              f"静默段最大开口达口播段的 {open_ratio:.0%}（仅记录）"))
        jaw = alive.get("silence_jaw_max")
        if jaw is not None and jaw >= t["silence_jaw_note"]:
            out.append(Defect("活性", "inhale_before_speech", "info",
                              f"静默段张颌幅度 {jaw:.2f}，像吸气（仅记录）"))
        if alive.get("silence_frozen"):
            out.append(Defect("活性", "silence_frozen", "info",
                              "静默段画面几乎不动（仅记录）"))

    # 口播段一次不眨眼仍然算问题——那是要进训练的部分
    blinks = alive.get("blink_count")
    if blinks == 0:
        severity = "minor" if detail_reliable else "info"
        suffix = "" if detail_reliable else f"（脸只有 {face_px:.0f}px，眨眼检测不可信）"
        out.append(Defect("活性", "no_blink", severity, f"全片一次没眨眼{suffix}"))
    elif alive.get("blink_rate_natural") is False:
        rate = alive.get("blink_rate_per_min")
        lo, hi = BLINK_RATE_NATURAL
        out.append(Defect("活性", "blink_rate_odd", "info",
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

    plaus = plausibility_metrics(track)
    plaus["face_px_median"] = round(track.median_face_px, 1)
    plaus["detail_metrics_reliable"] = bool(
        not np.isfinite(track.median_face_px)
        or track.median_face_px >= FACE_PX_DETAIL_FLOOR)

    metrics = {
        "体态": anatomy_metrics(track),
        "协调性": lip,
        "一致性": identity_metrics(track, reference_embedding=reference_embedding),
        "稳定性": stability_metrics(track),
        "合理性": plaus,
        "活性": aliveness_metrics(track, silence_seconds=measured_silence),
    }
    return Evaluation(
        available=True,
        metrics=metrics,
        defects=_collect_defects(metrics, silence_seconds=measured_silence,
                                 face_px=track.median_face_px),
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
