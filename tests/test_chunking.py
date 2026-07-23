"""分块规划的数学不变量测试。"""

import pytest

from scailswap.chunking import ChunkPlanner, ceil_to_4n1
from scailswap.errors import InvalidInputError


def test_ceil_to_4n1():
    assert ceil_to_4n1(1) == 1
    assert ceil_to_4n1(5) == 5
    assert ceil_to_4n1(6) == 9
    assert ceil_to_4n1(80) == 81
    assert ceil_to_4n1(81) == 81
    assert ceil_to_4n1(82) == 85


def test_default_plan_matches_scail2_training_config():
    """81 帧窗口 / 5 帧重叠 / 76 步进：228 帧应恰好 3 块（与社区节点一致）。"""
    chunks = ChunkPlanner().plan(228)
    assert len(chunks) == 3
    assert (chunks[0].src_start, chunks[0].src_end) == (0, 81)
    assert (chunks[1].src_start, chunks[1].src_end) == (76, 157)
    assert (chunks[2].src_start, chunks[2].src_end) == (152, 228)
    assert chunks[0].overlap == 0
    assert chunks[1].overlap == 5
    # 尾块 76 帧 → 补齐到 77（4n+1）
    assert chunks[2].src_length == 76
    assert chunks[2].gen_length == 77
    assert chunks[2].pad_frames == 1


@pytest.mark.parametrize("total", [1, 5, 30, 81, 82, 157, 500, 2880])
def test_plan_invariants(total):
    planner = ChunkPlanner()
    chunks = planner.plan(total)
    # 全覆盖无空洞：新增帧数之和 == 总帧数
    assert sum(c.new_frames for c in chunks) == total
    assert chunks[0].src_start == 0
    assert chunks[-1].src_end == total
    for i, c in enumerate(chunks):
        assert (c.gen_length - 1) % 4 == 0
        assert c.gen_length >= c.src_length
        if i > 0:
            prev = chunks[i - 1]
            # 相邻块共享恰好 overlap 帧源内容
            assert prev.src_end - c.src_start == c.overlap == planner.overlap
    # 每块提交帧数不超过窗口对齐值
    assert all(c.gen_length <= ceil_to_4n1(planner.window) for c in chunks)


def test_small_window_plan():
    chunks = ChunkPlanner(window=13, overlap=5).plan(30)
    assert sum(c.new_frames for c in chunks) == 30
    assert chunks[-1].src_end == 30


def test_invalid_params():
    with pytest.raises(InvalidInputError):
        ChunkPlanner(window=80)  # 非 4n+1
    with pytest.raises(InvalidInputError):
        ChunkPlanner(overlap=4)  # 非 4n+1
    with pytest.raises(InvalidInputError):
        ChunkPlanner(window=13, overlap=13)
    with pytest.raises(InvalidInputError):
        ChunkPlanner().plan(0)
