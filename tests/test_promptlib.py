#!/usr/bin/env python3
"""提示词积木。

调试台的下拉框全是从这里来的，所以这里的约束比别处严：
运营看不到代码，一条积木写坏了他们只会觉得「模型不行」。
"""

from __future__ import annotations

import pytest

import promptlib
from promptlib import (BASE, GROUPS, PRESETS, catalog, compose, preset)


class TestBlocks:
    def test_every_group_has_options(self):
        for name, items in GROUPS.items():
            assert items, f"{name} 是空的"

    def test_keys_unique_within_group(self):
        for name, items in GROUPS.items():
            keys = [b.key for b in items]
            assert len(keys) == len(set(keys)), f"{name} 有重复 key"

    def test_gesture_has_real_variety(self):
        """实测反馈是「手型太单一，只有摊手」，所以这一组必须够多。"""
        assert len(GROUPS["gesture"]) >= 6

    def test_gesture_keeps_baseline_for_comparison(self):
        # 去掉基线就没法判断新手势是变好还是变差
        assert any(b.key == "open-palm" for b in GROUPS["gesture"])

    def test_gestures_never_break_platform_rule(self):
        """平台硬要求手不遮挡面部颈部，没有一条手势可以叫模型抬手过肩。"""
        forbidden = ("举过头", "抬到头", "举高", "过肩", "遮住脸", "手放在脸")
        for b in GROUPS["gesture"]:
            for word in forbidden:
                assert word not in b.text, f"{b.key} 含违规描述：{word}"

    def test_unknown_group_raises(self):
        with pytest.raises(KeyError):
            promptlib.block("没这个组", "x")

    def test_unknown_key_lists_options(self):
        with pytest.raises(KeyError, match="open-palm"):
            promptlib.block("gesture", "没这个手势")


class TestCompose:
    def test_base_always_present(self):
        assert compose({}).startswith(BASE[:12])

    def test_includes_selected_block_text(self):
        text = compose({"gesture": "counting"})
        assert "数点" in text or "点数" in text

    def test_order_is_fixed(self):
        """语序固定，两次生成的差异才只来自内容。"""
        blocks = {"posture": "still", "gesture": "down",
                  "expression": "warm", "negative": "standard"}
        text = compose(blocks)
        assert text.index("躯干") < text.index("垂放")

    def test_empty_block_text_skipped(self):
        # speech=plain 和 negative=none 的正文是空的，不该留下多余空格
        text = compose({"speech": "plain", "negative": "none"})
        assert "  " not in text

    def test_extra_appended_last(self):
        text = compose({"gesture": "down"}, extra="额外一句")
        assert text.endswith("额外一句")

    def test_extra_blank_ignored(self):
        assert compose({}, extra="   ") == compose({})

    def test_unknown_group_in_blocks_raises(self):
        with pytest.raises(KeyError):
            compose({"gesture": "不存在的手势"})

    def test_ignores_unrelated_keys(self):
        # 前端可能多传字段，不该因此崩
        assert compose({"gesture": "down", "无关key": "x"})


class TestPresets:
    def test_all_presets_compose(self):
        for name in PRESETS:
            assert len(preset(name)) > 100

    def test_all_preset_blocks_are_valid(self):
        for name, spec in PRESETS.items():
            for group, key in spec["blocks"].items():
                promptlib.block(group, key)      # 不存在就抛

    def test_baseline_exists(self):
        assert "baseline" in PRESETS

    def test_presets_are_distinct(self):
        texts = {name: preset(name) for name in PRESETS}
        assert len(set(texts.values())) == len(texts), "有两个预设拼出了一样的提示词"

    def test_unknown_preset_raises(self):
        with pytest.raises(KeyError):
            preset("没这个预设")

    def test_every_preset_has_note(self):
        # 运营靠 note 判断什么时候用哪个
        for name, spec in PRESETS.items():
            assert spec["note"], f"{name} 没写用途说明"


class TestCatalog:
    def test_json_serialisable(self):
        import json

        json.dumps(catalog(), ensure_ascii=False)

    def test_shape_matches_frontend_expectations(self):
        data = catalog()
        assert set(data) == {"base", "groups", "presets", "photo_edits"}
        for group, spec in data["groups"].items():
            assert set(spec) == {"label", "hint", "options"}
            for opt in spec["options"]:
                assert set(opt) == {"key", "label", "text", "note"}
        for name, spec in data["presets"].items():
            assert set(spec) == {"label", "note", "blocks", "text"}

    def test_preset_text_matches_compose(self):
        """页面显示的和后端拼的必须一致，否则运营看到的不是实际发出去的。"""
        for name, spec in catalog()["presets"].items():
            assert spec["text"] == preset(name)


def test_neck_guard_block_exists():
    """实测有模型把颈部骨骼画得很显眼，得有一条针对它的禁止项。"""
    keys = [b.key for b in GROUPS["negative"]]
    assert "plus-neck" in keys
    text = promptlib.block("negative", "plus-neck").text
    assert "锁骨" in text or "颈部" in text


class TestBodyMode:
    """坐/站。运营常遇到「传了站姿照片但要坐着说」。"""

    def test_group_exists_with_keep_default(self):
        keys = [b.key for b in GROUPS["body"]]
        assert "keep" in keys and keys[0] == "keep", "默认必须是不改姿态"

    def test_covers_both_directions(self):
        keys = [b.key for b in GROUPS["body"]]
        assert "to-sitting" in keys and "to-standing" in keys

    def test_sitting_text_mentions_chair(self):
        assert "坐" in promptlib.block("body", "to-sitting").text

    def test_standing_text_mentions_standing(self):
        assert "站" in promptlib.block("body", "to-standing").text

    def test_keep_does_not_force_a_posture(self):
        text = promptlib.block("body", "keep").text
        assert "保持" in text

    def test_warning_is_honest_about_limits(self):
        """必须说清单图直出改不了姿态，否则运营会以为提示词没生效是 bug。"""
        warning = promptlib.BODY_MODE_WARNING
        assert "改不了" in warning or "不会" in warning
        assert "改照片" in warning

    def test_catalog_exposes_hint(self):
        assert catalog()["groups"]["body"]["hint"]

    def test_body_comes_first_in_composition(self):
        """坐/站决定整体取景，必须排在体态和手势之前。"""
        text = compose({"body": "to-sitting", "posture": "still",
                        "gesture": "down"})
        assert text.index("坐在椅子") < text.index("躯干")
        assert text.index("坐在椅子") < text.index("垂放")

    def test_all_presets_pin_body_explicitly(self):
        for name, spec in PRESETS.items():
            assert "body" in spec["blocks"], f"{name} 没写 body，会漂"


class TestPhotoEdits:
    """改照片姿态的提示词。比指望口播模型自己把人从站着变成坐着靠得住。"""

    def test_covers_both_directions(self):
        assert "to-sitting" in promptlib.PHOTO_EDITS
        assert "to-standing" in promptlib.PHOTO_EDITS

    def test_each_has_label_and_prompt(self):
        for name, spec in promptlib.PHOTO_EDITS.items():
            assert spec["label"] and spec["prompt"], name

    def test_prompts_pin_identity(self):
        """改姿态时必须锁住长相穿着，否则换出来的是另一个人。"""
        for name, spec in promptlib.PHOTO_EDITS.items():
            if name == "close-mouth":
                continue
            assert "长相" in spec["prompt"], name

    def test_close_mouth_preset_exists(self):
        # 平台要求开头闭口，参考图嘴张着会一路影响成片
        assert "close-mouth" in promptlib.PHOTO_EDITS

    def test_catalog_exposes_photo_edits(self):
        assert set(catalog()["photo_edits"]) == set(promptlib.PHOTO_EDITS)
