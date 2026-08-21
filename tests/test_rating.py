#!/usr/bin/env python3
"""成对比较 → 排名。这套东西要拿来给方案排序，算错了会选错模型。"""

from __future__ import annotations

import pytest

from lab.rating import bradley_terry, rank, win_rates


class TestBradleyTerry:
    def test_empty(self):
        assert bradley_terry([]) == {}

    def test_transitive_chain_recovers_order(self):
        comparisons = [("a", "b")] * 5 + [("b", "c")] * 5 + [("a", "c")] * 5
        scores = bradley_terry(comparisons)
        assert scores["a"] > scores["b"] > scores["c"]

    def test_scores_centred_on_zero(self):
        scores = bradley_terry([("a", "b")] * 3 + [("b", "a")] * 3)
        assert sum(scores.values()) == pytest.approx(0.0, abs=1e-6)

    def test_even_split_ties_scores(self):
        scores = bradley_terry([("a", "b")] * 4 + [("b", "a")] * 4)
        assert scores["a"] == pytest.approx(scores["b"], abs=1e-3)

    def test_undefeated_does_not_diverge(self):
        # 全胜项在无先验的 BT 里会跑到无穷，这里靠弱先验兜住
        scores = bradley_terry([("a", "b")] * 10)
        assert all(abs(v) < 20 for v in scores.values())
        assert scores["a"] > scores["b"]

    def test_self_comparison_ignored(self):
        assert bradley_terry([("a", "a")]) == {}

    def test_stronger_margin_gets_bigger_gap(self):
        close = bradley_terry([("a", "b")] * 5 + [("b", "a")] * 4)
        lopsided = bradley_terry([("a", "b")] * 9)
        assert (lopsided["a"] - lopsided["b"]) > (close["a"] - close["b"])

    def test_disconnected_groups_still_return(self):
        scores = bradley_terry([("a", "b"), ("c", "d")])
        assert set(scores) == {"a", "b", "c", "d"}


class TestWinRates:
    def test_counts(self):
        stats = win_rates([("a", "b"), ("a", "c"), ("b", "a")])
        assert stats["a"]["win"] == 2 and stats["a"]["loss"] == 1
        assert stats["a"]["games"] == 3
        assert stats["a"]["win_rate"] == pytest.approx(2 / 3, abs=0.01)


class TestRank:
    def test_sorted_desc_with_dense_ranks(self):
        rows = rank([("a", "b")] * 3 + [("b", "c")] * 3)
        assert [r["rank"] for r in rows] == [1, 2, 3]
        assert rows[0]["name"] == "a"

    def test_reports_game_counts(self):
        rows = rank([("a", "b")] * 2)
        assert all(r["games"] == 2 for r in rows)
