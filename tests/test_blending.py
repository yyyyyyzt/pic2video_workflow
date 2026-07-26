"""融合权重与颜色校正测试。"""

import numpy as np
import pytest

from scailswap.blending import (
    blend_overlap,
    cosine_weights,
    gaussian_weights,
    make_weights,
    reinhard_color_match,
)


@pytest.mark.parametrize("fn", [cosine_weights, gaussian_weights])
def test_weights_monotonic_and_bounded(fn):
    for n in (1, 5, 9, 16):
        w = fn(n)
        assert len(w) == n
        assert np.all(w >= 0.0) and np.all(w <= 1.0)
        assert np.all(np.diff(w) > 0)  # 严格单调递增
        # 对称性：前后权重互补
        assert np.allclose(w + w[::-1], 1.0, atol=1e-6)


def test_make_weights_curves():
    assert len(make_weights(5, "cosine")) == 5
    assert len(make_weights(5, "gaussian")) == 5
    with pytest.raises(ValueError):
        make_weights(5, "linear")


def test_blend_overlap_transitions_between_sources():
    black = [np.zeros((8, 8, 3), np.uint8)] * 5
    white = [np.full((8, 8, 3), 255, np.uint8)] * 5
    out = blend_overlap(black, white, curve="cosine")
    assert len(out) == 5
    values = [int(f.mean()) for f in out]
    assert all(b > a for a, b in zip(values, values[1:]))  # 逐帧变亮（淡入）
    assert values[0] < 128 < values[-1]


def test_blend_overlap_identical_inputs_is_noop():
    frames = [np.full((4, 4, 3), 100, np.uint8)] * 3
    out = blend_overlap(frames, frames)
    for f in out:
        assert np.allclose(f, 100, atol=1)


def test_reinhard_color_match_aligns_statistics():
    rng = np.random.default_rng(0)
    # 偏蓝的帧序列 vs 偏红的参考帧
    frames = [
        np.clip(rng.normal([180, 120, 60], 20, (32, 32, 3)), 0, 255).astype(np.uint8)
        for _ in range(4)
    ]
    reference = np.clip(rng.normal([60, 120, 180], 20, (32, 32, 3)), 0, 255).astype(np.uint8)
    matched = reinhard_color_match(frames, reference)
    assert len(matched) == 4
    # 校正后的整体均值应显著靠近参考帧
    before = abs(np.mean([f.mean(axis=(0, 1)) for f in frames], axis=0) - reference.mean(axis=(0, 1)))
    after = abs(np.mean([f.mean(axis=(0, 1)) for f in matched], axis=0) - reference.mean(axis=(0, 1)))
    assert np.all(after < before)


def test_reinhard_strength_zero_is_noop():
    frames = [np.full((8, 8, 3), 77, np.uint8)]
    ref = np.full((8, 8, 3), 200, np.uint8)
    out = reinhard_color_match(frames, ref, strength=0.0)
    assert np.array_equal(out[0], frames[0])
