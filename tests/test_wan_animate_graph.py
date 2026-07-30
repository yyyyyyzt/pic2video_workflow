"""Wan2.2-Animate 工作流图结构回归。

continue_motion 分块不得再用 trim_image 裁掉窗口头部，否则输出变成 77-5=72。
"""

from __future__ import annotations

from scailswap.config import WanAnimateConfig
from scailswap.engines.base import ChunkTask
from scailswap.engines.wan_animate_engine import WanAnimateEngine


def _task(*, with_anchor: bool) -> ChunkTask:
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
        mode="replacement",
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
