"""帧率自定义与任意时长的行为验证。

核心不变量：改写帧率时**时长必须保持不变**——否则回填原始音轨会音画错位。
"""

import os

import cv2
import numpy as np
import pytest

from scailswap import LongVideoProcessor, ProcessorParams
from scailswap.engines.fake_engine import FakeEngine
from scailswap.video_io import count_frames, probe_video, resample_fps, retime_fps, save_frame


def make_video(path: str, frames: int, fps: float, size: int = 64, audio: bool = False) -> str:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (size, size))
    for i in range(frames):
        frame = np.zeros((size, size, 3), np.uint8)
        frame[..., 2] = int(255 * i / max(frames - 1, 1))
        writer.write(frame)
    writer.release()
    if audio:
        import subprocess

        with_audio = path.replace(".mp4", "_a.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", path, "-f", "lavfi", "-i", f"sine=frequency=440:duration={frames / fps}",
             "-c:v", "copy", "-c:a", "aac", "-shortest", with_audio],
            check=True,
        )
        os.replace(with_audio, path)
    return path


def make_image(path: str, size: int = 64) -> str:
    cv2.imwrite(path, np.full((size, size, 3), 160, np.uint8))
    return path


# --------------------------------------------------------------------------- #
# video_io 层
# --------------------------------------------------------------------------- #
def test_resample_fps_preserves_duration(tmp_path):
    src = make_video(str(tmp_path / "src.mp4"), frames=60, fps=30.0)
    dst = str(tmp_path / "dst.mp4")
    resample_fps(src, dst, 16.0)

    src_info, dst_info = probe_video(src), probe_video(dst)
    assert abs(dst_info.fps - 16.0) < 0.1
    # 时长不变是硬要求（音画对齐的前提）
    assert abs(dst_info.duration - src_info.duration) < 0.15
    assert abs(dst_info.frame_count - 32) <= 2  # 2s × 16fps


def test_retime_fps_upscales_without_changing_duration(tmp_path):
    src = make_video(str(tmp_path / "src.mp4"), frames=32, fps=16.0)
    dst = str(tmp_path / "dst.mp4")
    retime_fps(src, dst, 30.0, interpolate=False)

    info = probe_video(dst)
    assert abs(info.fps - 30.0) < 0.1
    assert abs(info.duration - 2.0) < 0.15


def test_save_frame_extracts_last_frame(tmp_path):
    src = make_video(str(tmp_path / "src.mp4"), frames=20, fps=10.0)
    img = str(tmp_path / "anchor.png")
    save_frame(src, -1, img)

    assert os.path.exists(img)
    frame = cv2.imread(img)
    assert frame is not None and frame.shape[:2] == (64, 64)
    # 末帧的红色通道应接近最大值（构造时是随帧递增的）
    assert frame[..., 2].mean() > 200


# --------------------------------------------------------------------------- #
# processor 层
# --------------------------------------------------------------------------- #
@pytest.fixture()
def workspace(tmp_path):
    return (
        tmp_path,
        make_video(str(tmp_path / "driving.mp4"), frames=60, fps=30.0, audio=True),
        make_image(str(tmp_path / "face.png")),
    )


def test_target_fps_downsamples_generation(workspace):
    """降到 16fps 生成：帧数减半（省算力），成片时长与源一致。"""
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1, target_fps=16.0))
    out = processor.process(image, video, str(tmp / "out.mp4"))

    info = probe_video(out)
    assert abs(info.fps - 16.0) < 0.3
    assert abs(info.duration - 2.0) < 0.2          # 源 60/30=2s
    assert abs(info.frame_count - 32) <= 2
    # 送进模型的帧总量应约为源的一半
    assert sum(t.gen_length for t in engine.calls) < 60
    assert all(abs(t.fps - 16.0) < 0.01 for t in engine.calls)


def test_output_fps_differs_from_generation_fps(workspace):
    """16fps 生成 → 30fps 成片：时长仍不变。"""
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(
        engine, ProcessorParams(seed=1, target_fps=16.0, output_fps=30.0)
    )
    out = processor.process(image, video, str(tmp / "out.mp4"))

    info = probe_video(out)
    assert abs(info.fps - 30.0) < 0.3
    assert abs(info.duration - 2.0) < 0.25
    assert all(abs(t.fps - 16.0) < 0.01 for t in engine.calls)  # 生成侧仍是 16


def test_no_target_fps_follows_source(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1))
    out = processor.process(image, video, str(tmp / "out.mp4"))

    info = probe_video(out)
    assert abs(info.fps - 30.0) < 0.3
    assert info.frame_count == 60


def test_audio_preserved_and_aligned_after_fps_change(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1, target_fps=16.0))
    out = processor.process(image, video, str(tmp / "out.mp4"))

    info = probe_video(out)
    assert info.has_audio, "改写帧率后原始音轨必须保留"
    assert abs(info.duration - 2.0) < 0.25, "音画时长必须与源一致"


def test_cleanup_intermediate_removes_driving_chunks(workspace):
    """长素材必须能即用即删，否则几十 GB 中间文件撑爆磁盘。"""
    tmp, video, image = workspace
    work = str(tmp / "work")
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(
        engine, ProcessorParams(seed=1, cleanup_intermediate=True)
    )
    processor.process(image, video, str(tmp / "out.mp4"), work_dir=work)

    leftovers = [n for n in os.listdir(work) if n.endswith("_driving.mp4")]
    assert leftovers == []
    # 各块最终输出要保留（既是拼接源，也是断点续传/锚定链依据）
    assert [n for n in os.listdir(work) if n.endswith("_final.mp4")]


def test_keep_intermediate_when_disabled(workspace):
    tmp, video, image = workspace
    work = str(tmp / "work")
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(
        engine, ProcessorParams(seed=1, cleanup_intermediate=False)
    )
    processor.process(image, video, str(tmp / "out.mp4"), work_dir=work)
    assert [n for n in os.listdir(work) if n.endswith("_driving.mp4")]


def test_long_video_chunk_count_scales(tmp_path):
    """5 分钟 @16fps 的规划：约 67 块，无丢帧。"""
    from scailswap.chunking import ChunkPlanner

    total = int(5 * 60 * 16)  # 4800 帧
    chunks = ChunkPlanner(window=77, overlap=5).plan(total)
    assert sum(c.new_frames for c in chunks) == total
    assert chunks[-1].src_end == total
    assert len(chunks) == pytest.approx(total / 72, abs=2)


def test_chunk_codec_crf_option(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=13)
    processor = LongVideoProcessor(
        engine, ProcessorParams(seed=1, chunk_codec="crf", cleanup_intermediate=False)
    )
    out = processor.process(image, video, str(tmp / "out.mp4"), work_dir=str(tmp / "w"))
    assert count_frames(out) == 60


def test_invalid_chunk_codec_rejected(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"))
    processor = LongVideoProcessor(engine, ProcessorParams(chunk_codec="h265"))
    with pytest.raises(Exception, match="chunk_codec"):
        processor.process(image, video, str(tmp / "out.mp4"))
