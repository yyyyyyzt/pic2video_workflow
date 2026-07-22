"""视频工具：切分、重叠帧规划、crossfade 拼接、音频提取与合并。

本模块是长视频方案的工程核心。由于底层 API 单次调用受限于模型上下文窗口
（81 帧）与硬编码的 frame_load_cap（121 帧），无法直接生成 >5s 的视频，因此
必须：切分 -> 逐段推理 -> 按重叠帧平滑拼接 -> 合回原始音频。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# 视频探测
# ---------------------------------------------------------------------------
@dataclass
class VideoInfo:
    fps: float
    frame_count: int
    width: int
    height: int
    duration: float


def probe_video(path: str) -> VideoInfo:
    """读取视频基础信息（帧率 / 帧数 / 分辨率 / 时长）。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"无法打开视频：{path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    duration = frame_count / fps if fps else 0.0
    return VideoInfo(
        fps=fps,
        frame_count=frame_count,
        width=width,
        height=height,
        duration=duration,
    )


# ---------------------------------------------------------------------------
# 分段规划（重叠切割的核心）
# ---------------------------------------------------------------------------
@dataclass
class Segment:
    """一个待推理片段的帧区间描述。

    Attributes
    ----------
    index:
        片段序号（从 0 开始）。
    start:
        起始帧（含）。
    end:
        结束帧（不含）。即片段覆盖 [start, end)。
    """

    index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def plan_segments(
    total_frames: int,
    chunk_frames: int,
    overlap: int,
) -> List[Segment]:
    """把 ``total_frames`` 帧切成若干带重叠的片段。

    重叠帧原理
    ----------
    相邻两段共享 ``overlap`` 帧的源内容：第 i 段的末尾 ``overlap`` 帧，与第
    i+1 段的开头 ``overlap`` 帧对应同一批源视频帧。推理后，这段重叠区可用于
    crossfade（淡入淡出）平滑过渡，消除段与段之间的跳变。

    步进（stride）= chunk_frames - overlap，即每段实际「新增」的帧数。

    示例：chunk_frames=96, overlap=12 ->
        seg0 = [0, 96)
        seg1 = [84, 180)     # 与 seg0 重叠 [84,96)
        seg2 = [168, 264)    # 与 seg1 重叠 [168,180)
        ...
    最后一段会被裁剪到 total_frames，可能短于 chunk_frames。
    """
    if chunk_frames <= 0:
        raise ValueError("chunk_frames 必须为正数")
    if overlap < 0:
        raise ValueError("overlap 不能为负")
    if overlap >= chunk_frames:
        raise ValueError("overlap 必须小于 chunk_frames，否则片段无法推进")

    stride = chunk_frames - overlap
    segments: List[Segment] = []
    start = 0
    index = 0
    while start < total_frames:
        end = min(start + chunk_frames, total_frames)
        segments.append(Segment(index=index, start=start, end=end))
        index += 1
        # 已经到达结尾则停止（避免因重叠导致的无意义尾段）
        if end >= total_frames:
            break
        start += stride
    return segments


def plan_segments_for_mode(
    *,
    total_frames: int,
    fps: float,
    slice_mode: str = "normal",
    chunk_seconds: float = 3.5,
    overlap_frames: int = 12,
    frame_cap: int = 121,
) -> tuple[List[Segment], int]:
    """按切片模式规划片段，返回 (segments, effective_overlap)。"""
    if total_frames <= 0:
        raise ValueError("total_frames 必须为正数")

    if slice_mode == "single":
        return [Segment(index=0, start=0, end=total_frames)], 0

    if slice_mode == "halves":
        if total_frames == 1:
            return [Segment(index=0, start=0, end=1)], 0
        overlap = min(overlap_frames, max(1, total_frames // 4))
        mid = total_frames // 2
        return [
            Segment(index=0, start=0, end=min(total_frames, mid + overlap)),
            Segment(index=1, start=max(0, mid - overlap), end=total_frames),
        ], overlap

    chunk_frames = int(round(chunk_seconds * fps))
    chunk_frames = min(chunk_frames, frame_cap)
    if chunk_frames <= overlap_frames:
        raise ValueError(
            f"chunk_frames({chunk_frames}) 必须大于 overlap_frames({overlap_frames})"
        )
    return plan_segments(
        total_frames=total_frames,
        chunk_frames=chunk_frames,
        overlap=overlap_frames,
    ), overlap_frames


# ---------------------------------------------------------------------------
# 片段抽取（帧精确）
# ---------------------------------------------------------------------------
def extract_segment(
    input_path: str,
    segment: Segment,
    output_path: str,
    fps: float = 24.0,
) -> str:
    """用 OpenCV 帧精确地抽取 [start, end) 帧，写成一个短视频。

    使用 OpenCV 逐帧读写以保证帧数完全可控（ffmpeg 的按时间裁剪在可变帧率下
    容易差一两帧，会破坏后续重叠对齐）。
    """
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise IOError(f"无法打开视频：{input_path}")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    cap.set(cv2.CAP_PROP_POS_FRAMES, segment.start)
    written = 0
    for _ in range(segment.length):
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(frame)
        written += 1

    cap.release()
    writer.release()
    if written == 0:
        raise IOError(f"片段 {segment.index} 未抽取到任何帧")
    return output_path


# ---------------------------------------------------------------------------
# crossfade 拼接（重叠融合的核心）
# ---------------------------------------------------------------------------
def _read_all_frames(path: str) -> List[np.ndarray]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError(f"无法打开视频：{path}")
    frames: List[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    return frames


def crossfade_concat(
    segment_paths: List[str],
    overlap: int,
    output_path: str,
    fps: float = 24.0,
) -> str:
    """按重叠帧对多个片段输出做淡入淡出拼接，生成无缝长视频（无音频）。

    融合逻辑
    --------
    对相邻片段 A、B，它们各有 ``overlap`` 帧对应相同的源内容
    （A 的末尾 overlap 帧 vs B 的开头 overlap 帧）。逐帧线性混合：

        alpha = (k + 1) / (overlap + 1)          # k = 0 .. overlap-1
        blended[k] = (1 - alpha) * A_tail[k] + alpha * B_head[k]

    即在重叠区内让 A 逐渐淡出、B 逐渐淡入，从而消除接缝跳变。
    最终输出 = A 的非重叠部分 + 混合区 + B 的非重叠部分 + ...

    若 overlap==0 则退化为直接首尾相接。
    """
    if not segment_paths:
        raise ValueError("segment_paths 为空")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    writer: Optional[cv2.VideoWriter] = None
    prev_tail: List[np.ndarray] = []  # 上一段保留下来待与下一段混合的尾部帧
    last_index = len(segment_paths) - 1

    for i, path in enumerate(segment_paths):
        frames = _read_all_frames(path)
        if not frames:
            raise IOError(f"片段输出为空：{path}")

        if writer is None:
            h, w = frames[0].shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))

        # 该段实际可用的重叠帧数（末段之后无需保留尾部）
        eff_overlap = min(overlap, len(frames))

        if i == 0:
            # 第一段：写出除末尾重叠帧外的所有帧，尾部留给下一段混合
            if i == last_index:
                for f in frames:
                    writer.write(f)
            else:
                for f in frames[: len(frames) - eff_overlap]:
                    writer.write(f)
                prev_tail = frames[len(frames) - eff_overlap:]
            continue

        # 后续段：先与上一段尾部做 crossfade
        head = frames[:overlap]
        blend_n = min(len(prev_tail), len(head))
        for k in range(blend_n):
            alpha = (k + 1) / (blend_n + 1)
            blended = cv2.addWeighted(prev_tail[k], 1.0 - alpha, head[k], alpha, 0.0)
            writer.write(blended)

        # 写出该段中段部分
        if i == last_index:
            # 末段：混合区之后全部写完
            for f in frames[overlap:]:
                writer.write(f)
        else:
            # 中间段：保留末尾重叠帧给下一段
            eff_overlap = min(overlap, len(frames))
            middle = frames[overlap: len(frames) - eff_overlap]
            for f in middle:
                writer.write(f)
            prev_tail = frames[len(frames) - eff_overlap:]

    if writer is not None:
        writer.release()
    return output_path


# ---------------------------------------------------------------------------
# 音频提取与合并
# ---------------------------------------------------------------------------
def _run_ffmpeg(args: List[str]) -> None:
    """调用 ffmpeg 命令行，失败时抛出带 stderr 的异常。"""
    cmd = ["ffmpeg", "-y", *args]
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 执行失败：{' '.join(cmd)}\n{proc.stderr[-1000:]}"
        )


def has_audio_stream(path: str) -> bool:
    """判断视频是否含音频轨。"""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return bool(proc.stdout.strip())
    except FileNotFoundError:
        return False


def extract_audio(input_path: str, audio_path: str) -> Optional[str]:
    """从原始视频提取音频（AAC）。无音轨时返回 None。"""
    if not has_audio_stream(input_path):
        return None
    os.makedirs(os.path.dirname(os.path.abspath(audio_path)), exist_ok=True)
    _run_ffmpeg(["-i", input_path, "-vn", "-acodec", "aac", "-b:a", "192k", audio_path])
    return audio_path


def mux_audio(
    video_path: str,
    audio_path: Optional[str],
    output_path: str,
) -> str:
    """把音频合并回视频。audio_path 为 None 时仅重封装视频。"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if audio_path is None or not os.path.exists(audio_path):
        # 无音频：转封装为标准 H.264 mp4
        _run_ffmpeg(
            [
                "-i",
                video_path,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                output_path,
            ]
        )
        return output_path

    # 有音频：视频转 H.264（cv2 的 mp4v 兼容性一般），音频拷贝，按较短流对齐
    _run_ffmpeg(
        [
            "-i",
            video_path,
            "-i",
            audio_path,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            output_path,
        ]
    )
    return output_path
