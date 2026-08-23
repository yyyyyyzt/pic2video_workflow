#!/usr/bin/env python3
"""按需下载并缓存评测用的小模型。

两个模型都不进仓库（一个 3.7MB、一个 37MB），第一次用到时下载到 data/models/，
之后离线可用。下载失败不抛异常，让调用方降级——没有模型也应该能跑完流水线，
只是少几个指标。
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path

MODEL_DIR = Path(os.environ.get("AVATAR_MODEL_DIR", "data/models"))

# MediaPipe 官方托管的人脸关键点模型：478 个点 + 52 个 blendshape。
# blendshape 里直接有 eyeBlinkLeft/Right 和 jawOpen，省掉自己算 EAR 的误差。
FACE_LANDMARKER = (
    "face_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task",
)

# OpenCV Zoo 的 SFace：人脸识别 embedding，用来量化身份漂移。
SFACE = (
    "face_recognition_sface_2021dec.onnx",
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx",
)

# BlazePose 33 点：肩线、手腕、髋部。用 lite 版，体态指标不需要高精度，
# 而这一步是逐帧跑的，heavy 版会让 80 秒素材的评测时间翻好几倍。
POSE_LANDMARKER = (
    "pose_landmarker_lite.task",
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
)


def ensure(model: tuple[str, str], *, timeout: int = 120) -> Path | None:
    """返回本地模型路径，必要时下载。拿不到就返回 None。"""
    name, url = model
    dest = MODEL_DIR / name
    if dest.is_file() and dest.stat().st_size > 0:
        return dest

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        tmp.replace(dest)
        return dest
    except (urllib.error.URLError, OSError, TimeoutError):
        tmp.unlink(missing_ok=True)
        return None
