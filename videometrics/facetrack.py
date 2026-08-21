#!/usr/bin/env python3
"""逐帧跑一遍人脸关键点，把整段视频压成几条时间序列。

所有维度的指标都从这一次遍历里派生，避免每个指标各读一遍视频（10 秒 1080p
读一遍约 3 秒，读五遍就没法在 sweep 里默认开启了）。

产出的都是「归一化后的标量序列」，不含像素，可以直接序列化进 record.json，
也方便写单元测试——测试只需要构造这些序列，不需要真视频。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


class MetricsUnavailable(Exception):
    """缺依赖或缺模型，指标算不了。调用方应当降级而不是崩。"""


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
    embeddings: list = field(default_factory=list)   # 可选，SFace 向量

    @property
    def detect_rate(self) -> float:
        return float(self.detected.mean()) if self.frame_count else 0.0

    def times(self) -> np.ndarray:
        return np.arange(self.frame_count) / max(self.fps, 1e-6)


def _norm(value: float, denom: float) -> float:
    return float(value / denom) if denom > 1e-9 else 0.0


def track_faces(video, *, max_frames: int = 3000, want_embeddings: bool = True,
                stride: int = 1) -> FaceTrack:
    """遍历视频，返回逐帧信号。

    stride>1 时隔帧采样，用来在长视频上换速度；口型同步这类指标需要 stride=1。
    """
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
                        "mouth_sharpness")
    }
    detected: list[bool] = []
    embeddings: list = []
    prev_gray = None
    index = 0

    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        while index < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            if stride > 1 and index % stride:
                index += 1
                continue

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
            if not result.face_landmarks:
                detected.append(False)
                for key in ("mouth_open", "mouth_width", "blink", "jaw_open",
                            "face_size", "face_cx", "face_cy", "yaw_proxy",
                            "mouth_sharpness"):
                    cols[key].append(np.nan)
                index += 1
                continue

            detected.append(True)
            pts = result.face_landmarks[0]
            face_h = abs(pts[IDX_CHIN].y - pts[IDX_FOREHEAD].y)

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

            if recognizer is not None:
                emb = _embed(cv2, recognizer, frame, pts, width, height)
                if emb is not None:
                    embeddings.append(emb)

            index += 1

    cap.release()

    n = len(detected)
    return FaceTrack(
        fps=fps / max(stride, 1), frame_count=n, width=width, height=height,
        detected=np.array(detected, dtype=bool),
        embeddings=embeddings,
        **{k: np.array(v[:n], dtype=float) for k, v in cols.items()},
    )


def _make_recognizer(cv2):
    from . import models

    path = models.ensure(models.SFACE)
    if path is None:
        return None
    try:
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
