"""锚定模式（AnchorMode）对分块与融合策略的影响。

这是本轮改造的核心：引擎的锚定能力等级决定 processor 用什么策略，
用错了会直接导致鬼影（reference 模式做 crossfade）或身份漂移（none 模式硬拼）。
"""

import cv2
import numpy as np
import pytest

from scailswap import AnchorMode, LongVideoProcessor, ProcessorParams
from scailswap.engines.fake_engine import FakeEngine
from scailswap.errors import InvalidInputError
from scailswap.video_io import probe_video


def make_video(path: str, frames: int = 60, fps: float = 12.0, size: int = 64) -> str:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (size, size))
    for i in range(frames):
        frame = np.zeros((size, size, 3), np.uint8)
        frame[..., 0] = int(255 * i / max(frames - 1, 1))
        writer.write(frame)
    writer.release()
    return path


def make_image(path: str, size: int = 64) -> str:
    cv2.imwrite(path, np.full((size, size, 3), 180, np.uint8))
    return path


@pytest.fixture()
def workspace(tmp_path):
    return (
        tmp_path,
        make_video(str(tmp_path / "driving.mp4"), frames=60, fps=12.0),
        make_image(str(tmp_path / "face.png")),
    )


def test_latent_mode_uses_native_overlap_and_anchor_video(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(
        output_dir=str(tmp / "chunks"),
        anchor_mode=AnchorMode.LATENT,
        native_window=13,
        native_overlap=5,
    )
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1))
    out = processor.process(image, video, str(tmp / "out.mp4"))

    assert probe_video(out).frame_count == 60
    assert len(engine.calls) > 1
    # LATENT：传 anchor_video（latent 冻结），不传参考图
    assert engine.calls[0].anchor_video is None
    assert all(t.anchor_video is not None for t in engine.calls[1:])
    assert all(not t.anchor_images for t in engine.calls)


def test_reference_mode_disables_overlap_and_passes_anchor_images(workspace):
    """参考级锚定必须切成不重叠，并把上一块末帧作为附加参考图传下去。"""
    tmp, video, image = workspace
    engine = FakeEngine(
        output_dir=str(tmp / "chunks"),
        anchor_mode=AnchorMode.REFERENCE,
        native_window=13,
        native_overlap=5,
    )
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1))
    out = processor.process(image, video, str(tmp / "out.mp4"))

    assert probe_video(out).frame_count == 60
    assert len(engine.calls) > 1
    # 不传 latent 锚点（引擎拿不到），改传参考帧图片
    assert all(t.anchor_video is None for t in engine.calls)
    assert not engine.calls[0].anchor_images
    for task in engine.calls[1:]:
        assert len(task.anchor_images) == 1
        assert task.anchor_images[0].endswith(".png")


def test_reference_mode_ignores_user_overlap(workspace):
    """用户显式设了 overlap 也必须被覆盖为 0——否则融合会出鬼影。"""
    tmp, video, image = workspace
    engine = FakeEngine(
        output_dir=str(tmp / "chunks"),
        anchor_mode=AnchorMode.REFERENCE,
        native_window=13,
    )
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1, overlap_frames=9))
    window, overlap = processor._resolve_chunk_config(12.0)
    assert overlap == 0
    assert window == 13


def test_none_mode_rejects_long_video_with_actionable_message(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(
        output_dir=str(tmp / "chunks"),
        anchor_mode=AnchorMode.NONE,
        native_window=13,
    )
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1))
    with pytest.raises(InvalidInputError) as err:
        processor.process(image, video, str(tmp / "out.mp4"))
    # 报错要给出可执行的出路，而不是只说"不支持"
    assert "wan2.7-videoedit" in str(err.value)
    assert "max_duration_seconds" in str(err.value)


def test_none_mode_allows_single_chunk(workspace):
    tmp, video, image = workspace
    engine = FakeEngine(
        output_dir=str(tmp / "chunks"),
        anchor_mode=AnchorMode.NONE,
        native_window=81,
    )
    processor = LongVideoProcessor(engine, ProcessorParams(seed=1))
    out = processor.process(image, video, str(tmp / "out.mp4"))
    assert probe_video(out).frame_count == 60
    assert len(engine.calls) == 1


def test_window_follows_engine_native_config(workspace):
    """未显式指定时，窗口必须跟随引擎原生训练值（Animate=77 / SCAIL-2=81）。"""
    tmp, _, _ = workspace
    for window in (77, 81):
        engine = FakeEngine(output_dir=str(tmp / "chunks"), native_window=window)
        processor = LongVideoProcessor(engine, ProcessorParams())
        resolved_window, resolved_overlap = processor._resolve_chunk_config(16.0)
        assert resolved_window == window
        assert resolved_overlap == 5


def test_api_second_limit_shrinks_window(workspace):
    """托管 API 的秒数上限要能压小窗口，且结果仍是 4n+1。"""
    tmp, _, _ = workspace

    class CappedEngine(FakeEngine):
        def chunk_frames_limit(self, fps: float) -> int:
            return int(10.0 * fps) - 1  # 模拟 wan2.7-videoedit 的 10s 上限

    engine = CappedEngine(
        output_dir=str(tmp / "chunks"),
        anchor_mode=AnchorMode.REFERENCE,
        native_window=77,
    )
    processor = LongVideoProcessor(engine, ProcessorParams())
    # 16fps × 10s = 160 帧上限 > 原生 77，窗口应保持 77
    assert processor._resolve_chunk_config(16.0)[0] == 77
    # 30fps 下 10s = 300 帧，仍大于 77
    assert processor._resolve_chunk_config(30.0)[0] == 77
    # 5fps 下 10s 只有 49 帧，窗口必须被压到 ≤49 且为 4n+1
    window, _ = processor._resolve_chunk_config(5.0)
    assert window <= 49
    assert (window - 1) % 4 == 0
