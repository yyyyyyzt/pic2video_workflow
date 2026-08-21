#!/usr/bin/env python3
"""方案表与计价。计价这块坑最多：base_price 的单位因模型而异。"""

from __future__ import annotations

import pytest

from recipes import GROUPS, RECIPES, Recipe, build_payload, by_route, resolve


def _recipe(**kw) -> Recipe:
    base = dict(key="t", route="A", model="m", base_price=0.1,
                needs=("image", "audio"), label="测试")
    base.update(kw)
    return Recipe(**base)


class TestBillableSeconds:
    def test_uses_audio_length_by_default(self):
        assert _recipe().billable_seconds(37.5) == 37.5

    def test_route_b_duration_overrides_audio(self):
        # 路线 B 的时长由模型参数写死，和音频无关
        r = _recipe(route="B", params={"duration": 10})
        assert r.billable_seconds(60) == 10


class TestPriceFor:
    def test_per_second_unit(self):
        r = _recipe(price_unit="s", base_price=0.04)
        assert r.price_for(10) == pytest.approx(0.4)

    def test_five_second_blocks_round_up(self):
        r = _recipe(price_unit="5s", base_price=0.15)
        assert r.price_for(5) == pytest.approx(0.15)
        assert r.price_for(6) == pytest.approx(0.30)    # 6 秒要买 2 块
        assert r.price_for(10) == pytest.approx(0.30)

    def test_at_least_one_block(self):
        r = _recipe(price_unit="5s", base_price=0.15)
        assert r.price_for(0.5) == pytest.approx(0.15)

    def test_resolution_multiplier(self):
        cheap = _recipe(price_unit="5s", base_price=0.1, resolution="480p")
        pricey = _recipe(price_unit="5s", base_price=0.1, resolution="1080p")
        assert pricey.price_for(5) == pytest.approx(cheap.price_for(5) * 4)

    def test_run_unit_ignores_duration(self):
        r = _recipe(price_unit="run", base_price=0.5)
        assert r.price_for(5) == r.price_for(500)

    def test_measured_beats_formula(self):
        r = _recipe(price_unit="5s", base_price=99.0)
        assert r.price_for(10, measured_per_second=0.01) == pytest.approx(0.1)

    def test_verified_beats_formula(self):
        r = _recipe(price_unit="5s", base_price=99.0, verified_per_second=0.02)
        assert r.price_for(10) == pytest.approx(0.2)

    def test_measured_beats_verified(self):
        r = _recipe(verified_per_second=0.5)
        assert r.price_for(10, measured_per_second=0.01) == pytest.approx(0.1)


class TestResolve:
    def test_single_key(self):
        assert [r.key for r in resolve(["skyreels-std"])] == ["skyreels-std"]

    def test_group_expands(self):
        assert [r.key for r in resolve(["screen"])] == list(GROUPS["screen"])

    def test_comma_separated(self):
        keys = [r.key for r in resolve(["skyreels-std,omnihuman-15"])]
        assert keys == ["skyreels-std", "omnihuman-15"]

    def test_route_letter(self):
        assert all(r.route == "B" for r in resolve(["B"]))

    def test_all(self):
        assert len(resolve(["all"])) == len(RECIPES)

    def test_dedupes_preserving_order(self):
        keys = [r.key for r in resolve(["omnihuman-15", "screen", "omnihuman-15"])]
        assert keys.count("omnihuman-15") == 1
        assert keys[0] == "omnihuman-15"

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            resolve(["没有这个方案"])

    def test_blank_entries_ignored(self):
        assert resolve([" , "]) == []

    def test_route_is_case_insensitive(self):
        assert by_route("b") == by_route("B")


class TestBuildPayload:
    def test_maps_inputs(self):
        payload = build_payload(_recipe(), image_url="i", audio_url="a")
        assert payload["image"] == "i" and payload["audio"] == "a"

    def test_missing_required_input_raises(self):
        with pytest.raises(ValueError, match="image"):
            build_payload(_recipe(), audio_url="a")

    def test_empty_url_counts_as_missing(self):
        with pytest.raises(ValueError):
            build_payload(_recipe(), image_url="", audio_url="a")

    def test_prompt_required(self):
        r = _recipe(prompt_required=True)
        with pytest.raises(ValueError, match="prompt"):
            build_payload(r, image_url="i", audio_url="a")
        assert build_payload(r, image_url="i", audio_url="a", prompt="x")["prompt"] == "x"

    def test_negative_seed_omitted(self):
        payload = build_payload(_recipe(), image_url="i", audio_url="a", seed=-1)
        assert "seed" not in payload

    def test_zero_seed_kept(self):
        payload = build_payload(_recipe(), image_url="i", audio_url="a", seed=0)
        assert payload["seed"] == 0

    def test_resolution_injected(self):
        r = _recipe(resolution="720p")
        assert build_payload(r, image_url="i", audio_url="a")["resolution"] == "720p"

    def test_fixed_params_merged(self):
        r = _recipe(params={"mode": "replace"})
        assert build_payload(r, image_url="i", audio_url="a")["mode"] == "replace"

    def test_does_not_mutate_recipe_params(self):
        r = _recipe(params={"mode": "replace"})
        build_payload(r, image_url="i", audio_url="a", prompt="p")
        assert r.params == {"mode": "replace"}


def test_group_members_all_exist():
    for name, keys in GROUPS.items():
        missing = [k for k in keys if k not in RECIPES]
        assert not missing, f"组合 {name} 引用了不存在的方案 {missing}"


def test_upscalers_declare_video_input():
    for r in RECIPES.values():
        if r.route == "U":
            assert "video" in r.needs, f"{r.key} 是超分方案却不要 video"
