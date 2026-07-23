"""视频/音频 IO：探测、帧精确裁切、流式写出、音轨提取合并。

注意：这里的 ffmpeg 只用于封装层操作（转码、音轨合并），**不做任何时间轴
拼接**——所有跨块的内容一致性由模型锚定与像素融合完成。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Iterator, List, Optional

import cv2
import numpy as np

from .errors import InvalidInputError, ScailSwapError


@dataclass
class VideoInfo:
    fps: float
    frame_count: int
    width: int
    height: int
    duration: float
    has_audio: bool


def _ffprobe_json(path: str) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", path,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        raise ScailSwapError(f"ffprobe 失败：{path}\n{proc.stderr[-500:]}")
    return json.loads(proc.stdout)


def probe_video(path: str) -> VideoInfo:
    """探测帧率/帧数/分辨率/音轨。帧数用 OpenCV 复核（ffprobe 有时缺 nb_frames）。"""
    if not os.path.exists(path):
        raise InvalidInputError(f"视频文件不存在：{path}")
    meta = _ffprobe_json(path)
    vstream = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
    if vstream is None:
        raise InvalidInputError(f"文件中没有视频流：{path}")
    has_audio = any(s.get("codec_type") == "audio" for s in meta.get("streams", []))

    num, _, den = (vstream.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise InvalidInputError(f"OpenCV 无法打开视频：{path}")
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if fps <= 0:
        fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    cap.release()

    return VideoInfo(
        fps=fps,
        frame_count=frame_count,
        width=width,
        height=height,
        duration=frame_count / fps if fps else 0.0,
        has_audio=has_audio,
    )


def read_frames(path: str, start: int = 0, count: Optional[int] = None) -> List[np.ndarray]:
    """帧精确读取 [start, start+count) 帧（BGR uint8）。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise InvalidInputError(f"无法打开视频：{path}")
    if start > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    frames: List[np.ndarray] = []
    while count is None or len(frames) < count:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    return frames


def iter_frames(path: str) -> Iterator[np.ndarray]:
    """逐帧迭代（流式，不整段驻留内存）。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise InvalidInputError(f"无法打开视频：{path}")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            yield frame
    finally:
        cap.release()


def count_frames(path: str) -> int:
    cap = cv2.VideoCapture(path)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    return n


class StreamingVideoWriter:
    """流式视频写出器：逐帧 append，避免长视频整段驻留内存。

    先用 OpenCV 写 mp4v 中间文件，`close()` 后由调用方决定是否转码 H.264。
    """

    def __init__(self, path: str, fps: float, width: int, height: int) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.path = path
        self._writer = cv2.VideoWriter(
            path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
        )
        if not self._writer.isOpened():
            raise ScailSwapError(f"无法创建视频写出器：{path}")
        self.frames_written = 0
        self._size = (width, height)

    def write(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        if (w, h) != self._size:
            frame = cv2.resize(frame, self._size, interpolation=cv2.INTER_LANCZOS4)
        self._writer.write(frame)
        self.frames_written += 1

    def close(self) -> None:
        self._writer.release()


def run_ffmpeg(args: List[str]) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise ScailSwapError(f"ffmpeg 失败：{' '.join(cmd)}\n{proc.stderr[-1000:]}")


def write_chunk_video(
    frames: List[np.ndarray],
    path: str,
    fps: float,
    lossless: bool = True,
) -> str:
    """把一批帧写成短视频（分块输入 / 锚点视频用）。

    lossless=True 时用 x264 qp=0，尽量保真——锚点帧最终还会过一次 VAE 编码，
    轻微色度子采样损失可以忽略，但要避免普通有损压缩的块效应污染锚定。
    """
    if not frames:
        raise InvalidInputError("frames 为空")
    h, w = frames[0].shape[:2]
    tmp = path + ".raw.mp4"
    writer = StreamingVideoWriter(tmp, fps, w, h)
    for f in frames:
        writer.write(f)
    writer.close()
    codec_args = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    codec_args += ["-qp", "0"] if lossless else ["-crf", "16"]
    run_ffmpeg(["-i", tmp, *codec_args, path])
    os.remove(tmp)
    return path


def extract_audio(video_path: str, audio_path: str) -> Optional[str]:
    """提取原始音轨（AAC）。无音轨返回 None。"""
    if not probe_video(video_path).has_audio:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(audio_path)) or ".", exist_ok=True)
    run_ffmpeg(["-i", video_path, "-vn", "-acodec", "aac", "-b:a", "192k", audio_path])
    return audio_path


def mux_audio(video_path: str, audio_path: Optional[str], output_path: str) -> str:
    """H.264 转码 + 合并音轨。音视频按各自时间轴对齐（帧率与源一致，天然同步）。"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    if audio_path and os.path.exists(audio_path):
        run_ffmpeg([
            "-i", video_path, "-i", audio_path,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            output_path,
        ])
    else:
        run_ffmpeg(["-i", video_path, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", output_path])
    return output_path


def pick_resolution(src_w: int, src_h: int, tier: int = 512) -> tuple[int, int]:
    """按源视频宽高比选择生成分辨率。

    SCAIL-2 支持 512p / 704p 两档（H、W 都需被 32 整除）。按面积匹配档位、
    保持宽高比、四舍五入到 32 的倍数。
    """
    if src_w <= 0 or src_h <= 0:
        raise InvalidInputError("源视频分辨率非法")
    tier = 704 if tier >= 704 else 512
    # 目标面积：以 tier 为短边、16:9 为基准的面积档
    target_area = tier * tier * 16 / 9
    ratio = src_w / src_h
    h = (target_area / ratio) ** 0.5
    w = h * ratio
    w32 = max(32, int(round(w / 32)) * 32)
    h32 = max(32, int(round(h / 32)) * 32)
    return w32, h32
