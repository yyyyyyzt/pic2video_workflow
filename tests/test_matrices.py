#!/usr/bin/env python3
"""参数矩阵的展开顺序和命名。顺序会直接影响你先看到哪几条，不能乱。"""

from __future__ import annotations

import pytest

from matrices import MATRICES, Matrix, expand, read_prompt, resolve_matrix
from recipes import RECIPES


def _matrix(**kw) -> Matrix:
    base = dict(key="k", label="l", note="n", recipes=("skyreels-std",),
                silences=(2.0,), prompts=("alive",),
                script_file="prompts/tencent_general_script_10s.txt",
                voice="seed:felix_zh", upscale="")
    base.update(kw)
    return Matrix(**base)


class TestExpand:
    def test_cell_count_is_product(self):
        mx = _matrix(recipes=("a", "b"), silences=(1.0, 2.0, 3.0),
                     prompts=("alive", "strict"))
        assert len(expand(mx)) == 12

    def test_index_is_one_based_and_dense(self):
        cells = expand(_matrix(recipes=("a", "b"), silences=(1.0, 2.0)))
        assert [c.index for c in cells] == [1, 2, 3, 4]

    def test_silence_is_outermost_so_first_group_is_complete(self):
        # 先跑完 2s 的全部组合再换静默档，这样边生成边看时手上就有一组完整对比
        cells = expand(_matrix(recipes=("a", "b"), silences=(2.0, 1.0)))
        assert [c.silence for c in cells] == [2.0, 2.0, 1.0, 1.0]

    def test_ids_are_unique(self):
        cells = expand(MATRICES["screen10"])
        assert len({c.id for c in cells}) == len(cells)

    def test_id_encodes_all_three_axes(self):
        cell = expand(_matrix())[0]
        assert cell.id == "01-skyreels-std_sil2_alive"

    def test_fractional_silence_avoids_dot_in_id(self):
        # 文件名里不能出现点，会被当成扩展名
        cell = expand(_matrix(silences=(1.5,)))[0]
        assert cell.id.endswith("_sil1p5_alive")
        assert "." not in cell.id

    def test_prompt_file_recorded(self):
        assert expand(_matrix())[0].prompt_file.endswith("tencent_alive_prompt.txt")


class TestResolveMatrix:
    def test_known(self):
        assert resolve_matrix("screen10").key == "screen10"

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            resolve_matrix("不存在")

    def test_rejects_matrix_with_bad_recipe(self, monkeypatch):
        monkeypatch.setitem(MATRICES, "broken", _matrix(key="broken", recipes=("nope",)))
        with pytest.raises(KeyError, match="nope"):
            resolve_matrix("broken")


class TestReadPrompt:
    def test_strips_comments_and_joins(self):
        text = read_prompt("alive")
        assert "#" not in text
        assert "\n" not in text
        assert "眨眼" in text

    def test_alive_forbids_inhale(self):
        assert "深吸气" in read_prompt("alive")

    def test_unknown_key(self):
        with pytest.raises(KeyError):
            read_prompt("没有这个提示词")


def test_builtin_matrices_reference_real_recipes():
    for name, mx in MATRICES.items():
        for key in mx.recipes:
            assert key in RECIPES, f"{name} 引用了不存在的方案 {key}"


def test_builtin_matrices_silence_within_platform_range():
    # 腾讯要求开头静默 1~3 秒，矩阵不该跑到区间外去
    for name, mx in MATRICES.items():
        for silence in mx.silences:
            assert 1.0 <= silence <= 3.0, f"{name} 的静默 {silence} 超出 1~3s"


def test_prompt_files_exist():
    for name, mx in MATRICES.items():
        for key in mx.prompts:
            assert read_prompt(key), f"{name} 的提示词 {key} 是空的"
