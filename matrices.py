#!/usr/bin/env python3
"""对比试验的参数矩阵。

`avatar_lab.py run` 一次只跑一组参数。筛选开头吸气、眨眼、口型时，真正要看的是
「同一张图、同一段台词」下，模型 × 静默时长 × 提示词 的交叉结果。
这里把组合写死，sweep 一条命令展开，不再靠手工改参数。

命名约定：
  screen10          这次 10 秒检查的完整矩阵（默认）
  screen10-models   只换模型，静默/提示词固定，用来先花一笔钱看三个模型本身
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from recipes import RECIPES, resolve


@dataclass(frozen=True)
class Matrix:
    key: str
    label: str
    note: str
    recipes: tuple[str, ...]
    silences: tuple[float, ...]
    prompts: tuple[str, ...]          # 短名，对应 PROMPT_FILES
    script_file: str
    voice: str
    upscale: str                      # 空字符串 = 10 秒筛选不超分，省时间和钱
    still_head: bool = False
    profile: str = "tencent-general"
    tts_speed: float = 1.0
    tts_emotion: str = "neutral"


@dataclass(frozen=True)
class Cell:
    id: str
    index: int
    recipe: str
    silence: float
    prompt_key: str
    prompt_file: str


# 提示词短名 → 文件。alive 是默认观感：静默段要像活人（眨眼、微动），不要吸气。
PROMPT_FILES = {
    "alive": "prompts/tencent_alive_prompt.txt",
    "strict": "prompts/tencent_strict_prompt.txt",
}


MATRICES: dict[str, Matrix] = {
    "screen10": Matrix(
        key="screen10",
        label="10 秒筛选 · 模型 × 静默 × 提示词",
        note="完整对比。静默 2 秒是平台目标，所以最先跑 2s 切片，方便边生成边看。",
        recipes=("skyreels-std", "infinitetalk-fast", "omnihuman-15"),
        silences=(2.0, 1.0, 3.0),
        prompts=("alive", "strict"),
        script_file="prompts/tencent_general_script_10s.txt",
        voice="seed:felix_zh",
        upscale="",
    ),
    "screen10-models": Matrix(
        key="screen10-models",
        label="10 秒筛选 · 只换模型",
        note="三个模型各一条，静默 2s + alive 提示词。想先花最少的钱看模型本身时用。",
        recipes=("skyreels-std", "infinitetalk-fast", "omnihuman-15"),
        silences=(2.0,),
        prompts=("alive",),
        script_file="prompts/tencent_general_script_10s.txt",
        voice="seed:felix_zh",
        upscale="",
    ),
}


def prompt_path(key: str) -> Path:
    if key not in PROMPT_FILES:
        raise KeyError(f"未知提示词 {key!r}，可选：{', '.join(PROMPT_FILES)}")
    return Path(PROMPT_FILES[key])


def read_prompt(key: str) -> str:
    path = prompt_path(key)
    if not path.is_file():
        raise FileNotFoundError(f"找不到提示词文件 {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    return " ".join(l.strip() for l in lines
                    if l.strip() and not l.strip().startswith("#"))


def expand(matrix: Matrix) -> list[Cell]:
    """笛卡尔积。顺序：静默（2s 优先）→ 提示词 → 模型，同一对比组紧挨着。"""
    cells: list[Cell] = []
    n = 0
    for silence in matrix.silences:
        for prompt_key in matrix.prompts:
            for recipe in matrix.recipes:
                n += 1
                sil = f"{silence:g}".replace(".", "p")
                cell_id = f"{n:02d}-{recipe}_sil{sil}_{prompt_key}"
                cells.append(Cell(
                    id=cell_id, index=n, recipe=recipe, silence=silence,
                    prompt_key=prompt_key, prompt_file=PROMPT_FILES[prompt_key],
                ))
    return cells


def resolve_matrix(key: str) -> Matrix:
    if key not in MATRICES:
        raise KeyError(
            f"未知矩阵 {key!r}。可选：{', '.join(MATRICES)}。"
            f"查看：python3 avatar_lab.py sweep --list"
        )
    mx = MATRICES[key]
    missing = [k for k in mx.recipes if k not in RECIPES]
    if missing:
        raise KeyError(f"矩阵 {key} 引用了不存在的方案：{', '.join(missing)}")
    return mx


def recipes_of(matrix: Matrix):
    return resolve(list(matrix.recipes))
