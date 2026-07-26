"""LongVideoProcessor 全链路集成测试（fake 引擎，无 GPU）。

验证：分块调度、锚定链传递、断点续传、融合拼接、帧数/帧率保持、进度回调。
"""

import os

import cv2
import numpy as np
import pytest

from scailswap import LongVideoProcessor, ProcessorParams
from scailswap.engines.fake_engine import FakeEngine
from scailswap.errors import InvalidInputError
from scailswap.video_io import probe_video


def make_video(path: str, frames: int = 30, fps: float = 12.0, size: int = 64) -> str:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (size, size))
    for i in range(frames):
        frame = np.zeros((size, size, 3), np.uint8)
        frame[..., 0] = int(255 * i / frames)  # 随时间变化的蓝色渐变，便于核对帧序
        cv2.circle(frame, (8 + i, size // 2), 6, (0, 0, 255), -1)
        writer.write(frame)
    writer.release()
    return path


def make_image(path: str, size: int = 64) -> str:
    img = np.full((size, size, 3), 200, np.uint8)
    cv2.circle(img, (size // 2, size // 2), 16, (30, 30, 220), -1)
    cv2.imwrite(path, img)
    return path


@pytest.fixture()
def workspace(tmp_path):
    video = make_video(str(tmp_path / "driving.mp4"), frames=30, fps=12.0)
    image = make_image(str(tmp_path / "face.png"))
    return tmp_path, video, image


def small_params(**overrides) -> ProcessorParams:
    defaults = dict(window_frames=13, overlap_frames=5, seed=42)
    defaults.update(overrides)
    return ProcessorParams(**defaults)


def test_end_to_end_pipeline(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"))
    events = []
    processor = LongVideoProcessor(engine, small_params())
    out = processor.process(
        source_image=image,
        driving_video=video,
        output_path=str(tmp / "final.mp4"),
        on_progress=events.append,
    )

    assert os.path.exists(out)
    info = probe_video(out)
    # 帧数与帧率与源严格一致
    assert info.frame_count == 30
    assert abs(info.fps - 12.0) < 0.1

    # 锚定链：除首块外每个任务都带 anchor_video
    assert len(engine.calls) >= 3
    assert engine.calls[0].anchor_video is None
    assert all(t.anchor_video is not None for t in engine.calls[1:])
    assert all(t.anchor_frames == 5 for t in engine.calls[1:])
    # 每块提交帧数满足 4n+1
    assert all((t.gen_length - 1) % 4 == 0 for t in engine.calls)

    # 进度回调：单调递增且到达 100
    percents = [e.percent for e in events]
    assert percents == sorted(percents)
    assert percents[-1] == 100.0
    stages = {e.stage for e in events}
    assert {"prepare", "generate", "assemble", "audio", "done"} <= stages


def test_resume_skips_done_chunks(workspace):
    tmp, video, image = workspace
    work_dir = str(tmp / "work")
    out_path = str(tmp / "final.mp4")

    engine1 = FakeEngine(output_dir=str(tmp / "chunks"))
    LongVideoProcessor(engine1, small_params()).process(
        image, video, out_path, work_dir=work_dir
    )
    first_calls = len(engine1.calls)
    assert first_calls >= 3

    # 二次运行：断点续传应跳过所有已完成块，不再调用引擎
    engine2 = FakeEngine(output_dir=str(tmp / "chunks"))
    LongVideoProcessor(engine2, small_params()).process(
        image, video, out_path, work_dir=work_dir
    )
    assert len(engine2.calls) == 0


def test_unanchored_engine_rejects_long_video(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"))
    engine.supports_anchor = False  # 模拟 fal 引擎
    processor = LongVideoProcessor(engine, small_params())
    with pytest.raises(InvalidInputError, match="锚定"):
        processor.process(image, video, str(tmp / "final.mp4"))


def test_unanchored_engine_allows_single_chunk(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"))
    engine.supports_anchor = False
    # 窗口足够容纳整段（30 帧 < 81），单块提交合法
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1))
    out = processor.process(image, video, str(tmp / "final.mp4"))
    assert probe_video(out).frame_count == 30
    assert len(engine.calls) == 1
    assert engine.calls[0].anchor_video is None


def test_max_duration_cap(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(output_dir=str(tmp / "chunks"))
    processor = LongVideoProcessor(engine, small_params(max_duration_seconds=1.0))
    out = processor.process(image, video, str(tmp / "final.mp4"))
    assert probe_video(out).frame_count == 12  # 1s @ 12fps
