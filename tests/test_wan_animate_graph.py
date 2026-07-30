"""Wan2.2-Animate 工作流图结构回归。

- continue_motion 分块不得再用 trim_image 裁掉窗口头部，否则输出变成 77-5=72。
- replacement 掩码必须走 RT-DETR 框 + SAM3_Detect（box），不能用会空掩码的
  SAM3_VideoTrack 文本检测。
"""

from __future__ import annotations

from scailswap.config import WanAnimateConfig
from scailswap.engines.base import ChunkTask
from scailswap.engines.wan_animate_engine import WanAnimateEngine


def _task(*, with_anchor: bool, mode: str = "replacement") -> ChunkTask:
    return ChunkTask(
        index=1 if with_anchor else 0,
        driving_video="/tmp/drv.mp4",
        reference_image="/tmp/ref.png",
        gen_length=77,
        width=512,
        height=512,
        fps=16.0,
        prompt="test",
        negative_prompt="",
        seed=0,
        steps=6,
        cfg=1.0,
        shift=5.0,
        mode=mode,
        anchor_video="/tmp/anchor.mp4" if with_anchor else None,
        anchor_frames=5,
    )


def test_wan_animate_graph_keeps_continue_motion_frames():
    engine = WanAnimateEngine(WanAnimateConfig(base_url="http://127.0.0.1:8188"))
    graph = engine._build_graph(
        _task(with_anchor=True),
        driving_name="drv.mp4",
        reference_name="ref.png",
        anchor_name="anchor.mp4",
    )

    assert "continue_motion" in graph["animate"]["inputs"]
    assert graph["trim_latent"]["class_type"] == "TrimVideoLatent"
    assert graph["trim_latent"]["inputs"]["trim_amount"] == ["animate", 3]
    assert "drop_anchor_frames" not in graph
    assert not any(n.get("class_type") == "ImageFromBatch" for n in graph.values())
    assert graph["create_video"]["inputs"]["images"] == ["decode", 0]


def test_wan_animate_graph_first_chunk_still_trims_ref_latent_only():
    engine = WanAnimateEngine(WanAnimateConfig(base_url="http://127.0.0.1:8188"))
    graph = engine._build_graph(
        _task(with_anchor=False),
        driving_name="drv.mp4",
        reference_name="ref.png",
        anchor_name=None,
    )
    assert "continue_motion" not in graph["animate"]["inputs"]
    assert graph["create_video"]["inputs"]["images"] == ["decode", 0]


def test_replacement_mask_uses_rtdetr_boxes_and_sam3_detect():
    engine = WanAnimateEngine(WanAnimateConfig(base_url="http://127.0.0.1:8188"))
    graph = engine._build_graph(
        _task(with_anchor=False, mode="replacement"),
        driving_name="drv.mp4",
        reference_name="ref.png",
        anchor_name=None,
    )
    assert "sam3_track" not in graph
    assert "sam3_cond" not in graph
    assert graph["sam3_detect"]["class_type"] == "SAM3_Detect"
    assert graph["sam3_detect"]["inputs"]["bboxes"] == ["person_detect", 0]
    assert "conditioning" not in graph["sam3_detect"]["inputs"]
    assert graph["character_mask"]["class_type"] == "GrowMask"
    assert graph["animate"]["inputs"]["character_mask"] == ["character_mask", 0]
    assert graph["animate"]["inputs"]["background_video"] == ["driving_frames", 0]


def test_animation_mode_skips_character_mask():
    engine = WanAnimateEngine(WanAnimateConfig(base_url="http://127.0.0.1:8188"))
    graph = engine._build_graph(
        _task(with_anchor=False, mode="animation"),
        driving_name="drv.mp4",
        reference_name="ref.png",
        anchor_name=None,
    )
    assert "sam3_detect" not in graph
    assert "character_mask" not in graph["animate"]["inputs"]
    assert "background_video" not in graph["animate"]["inputs"]
