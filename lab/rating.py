#!/usr/bin/env python3
"""把「你觉得哪条更好」变成一个排名。

为什么不用 1~5 分打分：单个评分者给绝对分是不稳的。同一条视频今天给 3 分、
明天给 4 分很常见，而且分数会随着看过的样本漂移（前面看了几条烂的，后面
一条中等的就显得好）。AGI-Eval 那份报告能用 1~5 分是因为有 770 个人平均掉噪声，
我们只有你一个人。

成对比较（A 和 B 哪个好）对单评分者稳定得多，因为它只要求局部判断。
再用 Bradley-Terry 把成对结果拟合成一维能力值——这是体育排名和 LLM 竞技场
（Elo/Arena）用的同一套模型。

模型：P(i 胜 j) = exp(s_i) / (exp(s_i) + exp(s_j))
用 minorization-maximization 迭代求 s，比梯度下降稳，不需要调学习率。
"""

from __future__ import annotations

import math
from collections import defaultdict


def bradley_terry(comparisons: list[tuple[str, str]], *,
                  iterations: int = 200, tol: float = 1e-9) -> dict[str, float]:
    """comparisons 是 (胜者, 败者) 列表，返回 {名字: 分数}。

    分数是对数尺度，已平移到均值 0；差 1 分约等于 73% 胜率。
    没有出现在任何比较里的名字不会出现在结果中。
    """
    wins: dict[str, float] = defaultdict(float)
    pair_count: dict[tuple[str, str], float] = defaultdict(float)
    valid = [(w, l) for w, l in comparisons if w != l]
    for winner, loser in valid:
        wins[winner] += 1.0
        key = (winner, loser) if winner < loser else (loser, winner)
        pair_count[key] += 1.0

    # 参与者只从有效比较里取：自我比较不构成信息，不该凭空多出一个参赛项
    items = sorted({name for pair in valid for name in pair})
    if not items:
        return {}

    # 加一个极弱的先验，避免全胜/全败的项发散到无穷
    strength = {name: 1.0 for name in items}
    prior = 0.5

    for _ in range(iterations):
        updated = {}
        for name in items:
            denominator = prior
            for (a, b), n in pair_count.items():
                if name not in (a, b):
                    continue
                other = b if name == a else a
                denominator += n / (strength[name] + strength[other])
            numerator = wins[name] + prior
            updated[name] = numerator / denominator if denominator > 0 else strength[name]

        # 归一化，防止整体一起放大缩小
        scale = sum(updated.values()) / len(updated)
        updated = {k: v / scale for k, v in updated.items()}
        delta = max(abs(updated[k] - strength[k]) for k in items)
        strength = updated
        if delta < tol:
            break

    scores = {k: math.log(max(v, 1e-12)) for k, v in strength.items()}
    mean = sum(scores.values()) / len(scores)
    return {k: round(v - mean, 4) for k, v in scores.items()}


def win_rates(comparisons: list[tuple[str, str]]) -> dict[str, dict]:
    """每个名字的胜负场次，用来判断某一项的比较次数够不够。"""
    stats: dict[str, dict] = defaultdict(lambda: {"win": 0, "loss": 0})
    for winner, loser in comparisons:
        if winner == loser:
            continue
        stats[winner]["win"] += 1
        stats[loser]["loss"] += 1
    for name, s in stats.items():
        total = s["win"] + s["loss"]
        s["games"] = total
        s["win_rate"] = round(s["win"] / total, 3) if total else None
    return dict(stats)


def rank(comparisons: list[tuple[str, str]]) -> list[dict]:
    """按 Bradley-Terry 分数从高到低排。"""
    scores = bradley_terry(comparisons)
    stats = win_rates(comparisons)
    rows = [{"name": name, "score": score,
             "win": stats.get(name, {}).get("win", 0),
             "loss": stats.get(name, {}).get("loss", 0),
             "games": stats.get(name, {}).get("games", 0)}
            for name, score in scores.items()]
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows
