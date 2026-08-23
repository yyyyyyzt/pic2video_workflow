#!/usr/bin/env python3
"""逐帧跑一遍人脸和身体关键点，把整段视频压成几条时间序列。

所有维度的指标都从这一次遍历里派生，避免每个指标各读一遍视频。

产出的都是「归一化后的标量序列」，不含像素，可以直接序列化进 record.json，
也方便写单元测试——测试只需要构造这些序列，不需要真视频。

关于速度：口型同步要求逐帧的人脸信号，不能抽帧。但身份向量（SFace）和体态
（BlazePose）都是慢变量，按 embed_stride / pose_stride 抽帧就够，
默认每 5 帧一次。实测 80 秒素材如果全部逐帧跑，一条要几分钟，
在 sweep 里逐格算完根本等不起。
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field

import numpy as np


class MetricsUnavailable(Exception):
    """缺依赖或缺模型，指标算不了。调用方应当降级而不是崩。"""


# mediapipe / TFLite / OpenCV 每次建图都会刷这几行，说的都是「用 xnnpack 跑」
# 「新图引擎暂不支持指定 target」这类无关紧要的事。sweep 里每格刷 6~8 行，
# 会把真正的日志埋掉。这些是 C++ 层直接写 fd 2 的，Python 的 logging 拦不住，
# 环境变量也不生效，只能在 fd 层面过滤。
_NOISE_MARKERS = (
    "inference_feedback_manager",
    "face_landmarker_graph",
    "pose_landmarker_graph",
    "XNNPACK delegate",
    "Logging before InitGoogle",
    "setPreferableTarget",
    "landmark_projection_calculator",
    "Targets are not supported",
    "feedback tensors",
)


def quiet_backends() -> None:
    """能用环境变量/API 压住的先压住。剩下的靠 filtered_stderr()。"""
    import os

    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    try:
        import cv2

        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except (ImportError, AttributeError):
        pass
    try:
        from absl import logging as absl_logging

        absl_logging.set_verbosity(absl_logging.ERROR)
    except ImportError:
        pass


@contextlib.contextmanager
def filtered_stderr():
    """把 fd 2 暂时接到临时文件，结束后只把**非噪声**行放回去。

    刻意不整个丢掉：真正的报错（比如模型文件损坏、显存不足）必须还能看到，
    否则排查问题时会一头雾水。
    """
    import tempfile

    saved = os.dup(2)
    with tempfile.TemporaryFile(mode="w+b") as sink:
        try:
            os.dup2(sink.fileno(), 2)
            yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            sink.seek(0)
            raw = sink.read().decode("utf-8", "replace")
            kept = [line for line in raw.splitlines()
                    if line.strip() and not any(m in line for m in _NOISE_MARKERS)]
            if kept:
                sys.stderr.write("\n".join(kept) + "\n")


# MediaPipe FaceMesh 的固定索引。改这些值等于换模型，不要随便动。
IDX_LIP_UPPER_INNER = 13
IDX_LIP_LOWER_INNER = 14
IDX_LIP_LEFT = 61
IDX_LIP_RIGHT = 291
IDX_FOREHEAD = 10
IDX_CHIN = 152
IDX_EYE_R_OUTER = 33
IDX_EYE_R_INNER = 133
IDX_EYE_L_INNER = 362
IDX_EYE_L_OUTER = 263
IDX_NOSE_TIP = 1


@dataclass
class FaceTrack:
    """一段视频的逐帧人脸信号。长度都等于 frame_count。"""

    fps: float
    frame_count: int
    width: int
    height: int

    detected: np.ndarray            # bool，这一帧有没有找到脸
    mouth_open: np.ndarray          # 内唇开口距离 / 脸高，0 表示闭合
    mouth_width: np.ndarray         # 嘴角间距 / 脸高
    blink: np.ndarray               # blendshape 眨眼分数，左右取均值，0~1
    jaw_open: np.ndarray            # blendshape 张颌分数，0~1
    face_size: np.ndarray           # 脸高 / 画面高，人物远近
    face_cx: np.ndarray             # 脸中心 x / 画面宽
    face_cy: np.ndarray             # 脸中心 y / 画面高
    yaw_proxy: np.ndarray           # 鼻尖相对两眼中点的水平偏移，转头代理量
    frame_mean: np.ndarray          # 整帧灰度均值，测曝光漂移
    frame_diff: np.ndarray          # 与上一帧的平均绝对差，测跳帧/冻结
    mouth_sharpness: np.ndarray     # 嘴部区域拉普拉斯方差，测牙齿糊不糊
    face_px: np.ndarray             # 脸高的像素数。低于百来像素时细节类指标不可信

    # 体态。按 pose_stride 抽帧，没抽到的帧是 NaN
    shoulder_l_x: np.ndarray
    shoulder_l_y: np.ndarray
    shoulder_r_x: np.ndarray
    shoulder_r_y: np.ndarray
    neck_edge_ratio: np.ndarray     # 颈部边缘能量 / 脸颊边缘能量
    wrist_above_shoulder: np.ndarray  # 0/1，手腕高过肩线

    embeddings: list = field(default_factory=list)   # 可选，SFace 向量

    @property
    def detect_rate(self) -> float:
        return float(self.detected.mean()) if self.frame_count else 0.0

    @property
    def median_face_px(self) -> float:
        finite = self.face_px[np.isfinite(self.face_px)]
        return float(np.median(finite)) if finite.size else float("nan")

    def times(self) -> np.ndarray:
        return np.arange(self.frame_count) / max(self.fps, 1e-6)


def _norm(value: float, denom: float) -> float:
    return float(value / denom) if denom > 1e-9 else 0.0


def track_faces(video, *, max_frames: int = 6000, want_embeddings: bool = True,
                want_pose: bool = True, embed_stride: int = 5,
                pose_stride: int = 5) -> FaceTrack:
    """遍历视频，返回逐帧信号。

    人脸信号逐帧算（口型同步需要）；身份向量和体态按 stride 抽帧，
    它们都是慢变量，逐帧算纯属浪费。
    """
    quiet_backends()
    try:
        import cv2
    except ImportError as exc:
        raise MetricsUnavailable("需要 opencv-python-headless：pip install -r requirements.txt") from exc
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
    except ImportError as exc:
        raise MetricsUnavailable(
            "需要 mediapipe：pip install mediapipe；Linux 上还要 apt install libegl1 libgles2"
        ) from exc

    from . import models

    model_path = models.ensure(models.FACE_LANDMARKER)
    if model_path is None:
        raise MetricsUnavailable(
            f"下载不到人脸关键点模型，可手动放到 {models.MODEL_DIR / models.FACE_LANDMARKER[0]}"
        )

    recognizer = _make_recognizer(cv2) if want_embeddings else None

    pose_path = models.ensure(models.POSE_LANDMARKER) if want_pose else None

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise MetricsUnavailable(f"打不开视频 {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    options = vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
        output_face_blendshapes=True,
    )

    cols: dict[str, list[float]] = {
        k: [] for k in ("mouth_open", "mouth_width", "blink", "jaw_open", "face_size",
                        "face_cx", "face_cy", "yaw_proxy", "frame_mean", "frame_diff",
                        "mouth_sharpness", "face_px",
                        "shoulder_l_x", "shoulder_l_y", "shoulder_r_x", "shoulder_r_y",
                        "neck_edge_ratio", "wrist_above_shoulder")
    }
    detected: list[bool] = []
    embeddings: list = []
    prev_gray = None
    index = 0

    face_only = ("mouth_open", "mouth_width", "blink", "jaw_open", "face_size",
                 "face_cx", "face_cy", "yaw_proxy", "mouth_sharpness", "face_px")
    pose_only = ("shoulder_l_x", "shoulder_l_y", "shoulder_r_x", "shoulder_r_y",
                 "neck_edge_ratio", "wrist_above_shoulder")

    pose_landmarker = None
    if pose_path is not None:
      with filtered_stderr():
        pose_landmarker = vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(pose_path)),
                running_mode=vision.RunningMode.VIDEO,
                num_poses=1,
            ))

    with filtered_stderr(), vision.FaceLandmarker.create_from_options(options) as landmarker:
        while index < max_frames:
            ok, frame = cap.read()
            if not ok:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            cols["frame_mean"].append(float(gray.mean()))
            if prev_gray is None or prev_gray.shape != gray.shape:
                cols["frame_diff"].append(0.0)
            else:
                cols["frame_diff"].append(float(np.abs(
                    gray.astype(np.int16) - prev_gray.astype(np.int16)).mean()))
            prev_gray = gray

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                int(index * 1000 / max(fps, 1e-6)),
            )
            pose_pts = None
            if pose_landmarker is not None and index % max(pose_stride, 1) == 0:
                pose_result = pose_landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                    int(index * 1000 / max(fps, 1e-6)))
                if pose_result.pose_landmarks:
                    pose_pts = pose_result.pose_landmarks[0]

            if not result.face_landmarks:
                detected.append(False)
                for key in face_only:
                    cols[key].append(np.nan)
                _append_pose(cols, cv2, gray, pose_pts, None, width, height, pose_only)
                index += 1
                continue

            detected.append(True)
            pts = result.face_landmarks[0]
            face_h = abs(pts[IDX_CHIN].y - pts[IDX_FOREHEAD].y)
            cols["face_px"].append(float(face_h * height))

            cols["mouth_open"].append(_norm(
                abs(pts[IDX_LIP_LOWER_INNER].y - pts[IDX_LIP_UPPER_INNER].y), face_h))
            cols["mouth_width"].append(_norm(
                abs(pts[IDX_LIP_RIGHT].x - pts[IDX_LIP_LEFT].x), face_h))
            cols["face_size"].append(float(face_h))
            cols["face_cx"].append(float((pts[IDX_LIP_LEFT].x + pts[IDX_LIP_RIGHT].x) / 2))
            cols["face_cy"].append(float((pts[IDX_FOREHEAD].y + pts[IDX_CHIN].y) / 2))

            eye_mid_x = (pts[IDX_EYE_R_OUTER].x + pts[IDX_EYE_L_OUTER].x) / 2
            cols["yaw_proxy"].append(_norm(pts[IDX_NOSE_TIP].x - eye_mid_x, face_h))

            if result.face_blendshapes:
                scores = {c.category_name: c.score for c in result.face_blendshapes[0]}
                cols["blink"].append(
                    (scores.get("eyeBlinkLeft", 0.0) + scores.get("eyeBlinkRight", 0.0)) / 2)
                cols["jaw_open"].append(scores.get("jawOpen", 0.0))
            else:
                cols["blink"].append(np.nan)
                cols["jaw_open"].append(np.nan)

            cols["mouth_sharpness"].append(
                _mouth_sharpness(cv2, gray, pts, width, height))

            _append_pose(cols, cv2, gray, pose_pts, pts, width, height, pose_only)

            if recognizer is not None and index % max(embed_stride, 1) == 0:
                emb = _embed(cv2, recognizer, frame, pts, width, height)
                if emb is not None:
                    embeddings.append(emb)

            index += 1

    cap.release()
    if pose_landmarker is not None:
        pose_landmarker.close()

    n = len(detected)
    return FaceTrack(
        fps=fps, frame_count=n, width=width, height=height,
        detected=np.array(detected, dtype=bool),
        embeddings=embeddings,
        **{k: np.array(v[:n], dtype=float) for k, v in cols.items()},
    )


def _append_pose(cols, cv2, gray, pose_pts, face_pts, width: int, height: int,
                 keys) -> None:
    """把这一帧的体态量塞进列。没有 pose 数据就补 NaN，保持所有列等长。"""
    if pose_pts is None:
        for key in keys:
            cols[key].append(np.nan)
        return

    from .anatomy import (POSE_SHOULDER_L, POSE_SHOULDER_R, POSE_WRIST_L,
                          POSE_WRIST_R)

    left = pose_pts[POSE_SHOULDER_L]
    right = pose_pts[POSE_SHOULDER_R]
    cols["shoulder_l_x"].append(float(left.x))
    cols["shoulder_l_y"].append(float(left.y))
    cols["shoulder_r_x"].append(float(right.x))
    cols["shoulder_r_y"].append(float(right.y))

    shoulder_y = (left.y + right.y) / 2
    wrists = [pose_pts[POSE_WRIST_L].y, pose_pts[POSE_WRIST_R].y]
    # y 轴向下为正，所以手腕 y 更小 = 手抬得更高
    cols["wrist_above_shoulder"].append(
        float(any(w < shoulder_y for w in wrists)))

    cols["neck_edge_ratio"].append(
        _neck_edge_ratio(cv2, gray, pose_pts, face_pts, width, height))


def _neck_edge_ratio(cv2, gray, pose_pts, face_pts, width: int, height: int) -> float:
    """颈部边缘能量 / 脸颊边缘能量。

    脸颊做基准是关键：它和颈部在同一张脸上、同一个光照下、同样是皮肤，
    所以比值能把「这段视频整体清晰度高」和「颈部真的长出了结构」区分开。
    比值明显大于 1 就说明颈部的纹理比脸颊还复杂——锁骨、颈筋被画出来了。
    """
    if face_pts is None:
        return float("nan")

    from .anatomy import POSE_SHOULDER_L, POSE_SHOULDER_R

    chin_y = face_pts[IDX_CHIN].y
    shoulder_y = (pose_pts[POSE_SHOULDER_L].y + pose_pts[POSE_SHOULDER_R].y) / 2
    if shoulder_y - chin_y < 0.02:
        return float("nan")

    cx = (pose_pts[POSE_SHOULDER_L].x + pose_pts[POSE_SHOULDER_R].x) / 2
    half = abs(pose_pts[POSE_SHOULDER_R].x - pose_pts[POSE_SHOULDER_L].x) * 0.18

    neck = _roi_laplacian(cv2, gray, cx - half, chin_y + (shoulder_y - chin_y) * 0.25,
                          cx + half, shoulder_y - (shoulder_y - chin_y) * 0.05,
                          width, height)

    # 脸颊参考区必须落在脸**内部**：从人脸外框推，而不是从嘴角往外推——
    # 往外推很容易越过脸缘落到头发或背景上，那里高频很多，比值会被压到 1 以下，
    # 看起来像「颈部特别平滑」，其实是参考区选错了。
    xs = [p.x for p in face_pts]
    ys = [p.y for p in face_pts]
    fx0, fx1 = min(xs), max(xs)
    fy0, fy1 = min(ys), max(ys)
    face_w, face_h = fx1 - fx0, fy1 - fy0
    face_cx = (fx0 + fx1) / 2
    cheek = _roi_laplacian(cv2, gray,
                           face_cx + face_w * 0.14, fy0 + face_h * 0.50,
                           face_cx + face_w * 0.36, fy0 + face_h * 0.74,
                           width, height)
    if not np.isfinite(neck) or not np.isfinite(cheek) or cheek < 1e-6:
        return float("nan")
    return float(neck / cheek)


def _roi_laplacian(cv2, gray, x0: float, y0: float, x1: float, y1: float,
                   width: int, height: int) -> float:
    px0, px1 = int(max(0.0, min(x0, x1)) * width), int(min(1.0, max(x0, x1)) * width)
    py0, py1 = int(max(0.0, min(y0, y1)) * height), int(min(1.0, max(y0, y1)) * height)
    if px1 - px0 < 8 or py1 - py0 < 8:
        return float("nan")
    return float(cv2.Laplacian(gray[py0:py1, px0:px1], cv2.CV_64F).var())


def _make_recognizer(cv2):
    from . import models

    path = models.ensure(models.SFACE)
    if path is None:
        return None
    try:
        with filtered_stderr():
            return cv2.FaceRecognizerSF.create(str(path), "")
    except cv2.error:
        return None


def _mouth_sharpness(cv2, gray, pts, width: int, height: int) -> float:
    """嘴部 ROI 的拉普拉斯方差。牙齿糊掉、口腔涂抹时会明显偏低。"""
    xs = [pts[i].x for i in (IDX_LIP_LEFT, IDX_LIP_RIGHT)]
    ys = [pts[i].y for i in (IDX_LIP_UPPER_INNER, IDX_LIP_LOWER_INNER)]
    pad_x = (max(xs) - min(xs)) * 0.25
    pad_y = max((max(ys) - min(ys)) * 1.5, 0.01)
    x0 = int(max(0.0, min(xs) - pad_x) * width)
    x1 = int(min(1.0, max(xs) + pad_x) * width)
    y0 = int(max(0.0, min(ys) - pad_y) * height)
    y1 = int(min(1.0, max(ys) + pad_y) * height)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return float("nan")
    roi = gray[y0:y1, x0:x1]
    return float(cv2.Laplacian(roi, cv2.CV_64F).var())


def _embed(cv2, recognizer, frame, pts, width: int, height: int):
    """用 SFace 取一帧的人脸向量。

    SFace 的 alignCrop 要的是 YuNet 的 15 维输出格式，这里从 MediaPipe 的关键点
    拼出同样的布局：bbox + 右眼/左眼/鼻尖/右嘴角/左嘴角 + 置信度。
    """
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    x0, x1 = min(xs) * width, max(xs) * width
    y0, y1 = min(ys) * height, max(ys) * height
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None

    def px(i):
        return pts[i].x * width, pts[i].y * height

    re_x, re_y = px(IDX_EYE_R_INNER)
    le_x, le_y = px(IDX_EYE_L_INNER)
    n_x, n_y = px(IDX_NOSE_TIP)
    rm_x, rm_y = px(IDX_LIP_LEFT)
    lm_x, lm_y = px(IDX_LIP_RIGHT)
    box = np.array([[x0, y0, x1 - x0, y1 - y0,
                     re_x, re_y, le_x, le_y, n_x, n_y,
                     rm_x, rm_y, lm_x, lm_y, 1.0]], dtype=np.float32)
    try:
        aligned = recognizer.alignCrop(frame, box)
        return recognizer.feature(aligned).flatten().copy()
    except cv2.error:
        return None
