#!/usr/bin/env python3
"""测试夹具。

分三档，靠 marker 区分，这样在没装全依赖的机器上也能跑一部分：

    (无标记)   纯函数，只要 python + numpy
    ffmpeg     需要 ffmpeg/ffprobe，用 lavfi 合成素材，不联网不花钱
    metrics    需要 opencv + mediapipe + 模型文件
    realface   需要一段真人/数字人视频，通过 AVATAR_TEST_VIDEO 指定

合成素材一律用 lavfi（testsrc2/color/sine/anullsrc），不往仓库里塞二进制。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def pytest_configure(config):
    config.addinivalue_line("markers", "ffmpeg: 需要 ffmpeg")
    config.addinivalue_line("markers", "metrics: 需要 opencv + mediapipe + 模型")
    config.addinivalue_line("markers", "realface: 需要一段真实人脸视频")


def pytest_collection_modifyitems(config, items):
    skip_ffmpeg = pytest.mark.skip(reason="没装 ffmpeg/ffprobe")
    for item in items:
        if "ffmpeg" in item.keywords and not HAS_FFMPEG:
            item.add_marker(skip_ffmpeg)


def _run(cmd: list[str]) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:6])}… 失败：{out.stderr[-500:]}")


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """一组合成素材。scope=session，因为编码这几条要几秒钟。"""
    if not HAS_FFMPEG:
        pytest.skip("没装 ffmpeg")
    d = tmp_path_factory.mktemp("media")

    # 明显不合规：480×832（比例 1.733 不是 9:16）、25fps、12 秒、有声
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc2=size=480x832:rate=25:duration=12",
          "-f", "lavfi", "-i", "sine=frequency=400:duration=12",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(d / "bad.mp4")])

    # 无音轨
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(d / "silent.mp4")])

    # 纯静音音频 3 秒 + 正弦 4 秒，用来测静默头检测
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "sine=frequency=400:duration=4",
          "-c:a", "libmp3lame", str(d / "speech.mp3")])

    # 一张纯色图，当角色图用
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "color=c=0x336699:s=480x832:d=1",
          "-frames:v", "1", str(d / "face.png")])
    return d


@pytest.fixture
def video_with_silent_head(media, tmp_path):
    """2 秒静音 + 4 秒正弦，配 6 秒画面。用来测静默头相关的判定。"""
    from mediaprep import prepend_silence

    padded = prepend_silence(media / "speech.mp3", tmp_path / "padded.mp3", 2.0)
    out = tmp_path / "with_head.mp4"
    _run(["ffmpeg", "-y", "-v", "error",
          "-f", "lavfi", "-i", "testsrc2=size=480x832:rate=25:duration=6",
          "-i", str(padded),
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(out)])
    return out


@pytest.fixture(scope="session")
def real_video():
    """真实数字人成片。没提供就跳过——合成素材里没有人脸，测不了人脸链路。"""
    path = os.environ.get("AVATAR_TEST_VIDEO")
    if not path or not Path(path).is_file():
        pytest.skip("需要设置 AVATAR_TEST_VIDEO 指向一段真实人脸视频")
    return Path(path)
