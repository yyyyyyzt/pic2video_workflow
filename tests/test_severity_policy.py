#!/usr/bin/env python3
"""严重度分级策略。

这些断言把「使用方确认过的事实」钉成测试，防止后面改阈值时不小心改回去：

  静默段不参与平台训练，只用来链接分割不同的动作 → 静默段问题只记录，不排序
  口型重要但不是决定性，观众不盯着嘴看               → 口型问题整体降一级
  整体协调（动作、肢体、颈部）才是重点               → 体态问题定得最严
"""

from __future__ import annotations

import numpy as np
import pytest

from videometrics.report import (FACE_PX_DETAIL_FLOOR, SEVERITY_ORDER, THRESHOLDS,
                                 _collect_defects)


def metrics(**blocks):
    base = {"体态": {}, "协调性": {}, "一致性": {},
            "稳定性": {}, "合理性": {}, "活性": {"blink_count": 3}}
    base.update(blocks)
    return base


def codes(defects, severity=None):
    return [d.code for d in defects if severity is None or d.severity == severity]


class TestSilenceIsInformationalOnly:
    """静默段的所有判定都不该进 major/minor。"""

    def _silence_metrics(self):
        return metrics(
            协调性={"silence_measured_s": 2.0},
            活性={"blink_count": 3,
                 "silence_vs_speech_mouth_ratio": 1.31,
                 "silence_open_vs_speech_ratio": 0.92,
                 "silence_jaw_max": 0.25,
                 "silence_frozen": True},
        )

    def test_mouth_active_is_info(self):
        found = [d for d in _collect_defects(self._silence_metrics(), silence_seconds=2.0)
                 if d.code == "silence_mouth_active"]
        assert found and found[0].severity == "info"

    def test_mouth_not_closed_is_info(self):
        found = [d for d in _collect_defects(self._silence_metrics(), silence_seconds=2.0)
                 if d.code == "silence_mouth_not_closed"]
        assert found and found[0].severity == "info"

    def test_inhale_is_info(self):
        found = [d for d in _collect_defects(self._silence_metrics(), silence_seconds=2.0)
                 if d.code == "inhale_before_speech"]
        assert found and found[0].severity == "info"

    def test_frozen_is_info(self):
        found = [d for d in _collect_defects(self._silence_metrics(), silence_seconds=2.0)
                 if d.code == "silence_frozen"]
        assert found and found[0].severity == "info"

    def test_no_silence_defect_reaches_major_or_minor(self):
        defects = _collect_defects(self._silence_metrics(), silence_seconds=2.0)
        silence_codes = {"silence_mouth_active", "silence_mouth_not_closed",
                         "inhale_before_speech", "silence_frozen",
                         "no_blink_in_silence"}
        graded = {d.code for d in defects if d.severity in ("major", "minor")}
        assert not (graded & silence_codes)


class TestLipsyncDowngraded:
    def test_moderately_low_corr_is_minor_not_major(self):
        found = [d for d in _collect_defects(metrics(协调性={"corr_best": 0.18}),
                                             silence_seconds=0)
                 if d.code == "lipsync_weak"]
        assert found and found[0].severity == "minor"

    def test_near_zero_corr_still_major(self):
        found = [d for d in _collect_defects(metrics(协调性={"corr_best": 0.02}),
                                             silence_seconds=0)
                 if d.code == "lipsync_weak"]
        assert found and found[0].severity == "major"

    def test_150ms_offset_is_minor(self):
        found = [d for d in _collect_defects(
            metrics(协调性={"corr_best": 0.6, "lag_ms": -160.0}), silence_seconds=0)
            if d.code == "lipsync_lag"]
        assert found and found[0].severity == "minor"

    def test_large_offset_still_major(self):
        found = [d for d in _collect_defects(
            metrics(协调性={"corr_best": 0.6, "lag_ms": 350.0}), silence_seconds=0)
            if d.code == "lipsync_lag"]
        assert found and found[0].severity == "major"


class TestBodyIsTopPriority:
    def test_body_thresholds_stricter_than_lipsync_relatively(self):
        # 体态只要有可见波动就报，而口型要低到 0.25 以下才报
        assert THRESHOLDS["shoulder_tilt_range_minor"] <= 6.0
        assert THRESHOLDS["lipsync_corr_minor"] <= 0.25

    def test_mild_body_wobble_already_flagged(self):
        found = _collect_defects(metrics(体态={"shoulder_tilt_range": 7.0}),
                                 silence_seconds=0)
        assert "shoulder_unstable" in codes(found)


class TestLowResolutionGuard:
    def test_no_blink_downgraded_on_small_face(self):
        """384×576 的输出脸太小，眨眼检测不可信，不该拿它扣分。"""
        found = [d for d in _collect_defects(
            metrics(活性={"blink_count": 0}), silence_seconds=0,
            face_px=FACE_PX_DETAIL_FLOOR - 60)
            if d.code == "no_blink"]
        assert found and found[0].severity == "info"
        assert "不可信" in found[0].detail

    def test_no_blink_is_minor_on_large_face(self):
        found = [d for d in _collect_defects(
            metrics(活性={"blink_count": 0}), silence_seconds=0,
            face_px=FACE_PX_DETAIL_FLOOR + 200)
            if d.code == "no_blink"]
        assert found and found[0].severity == "minor"

    def test_unknown_face_size_assumes_reliable(self):
        found = [d for d in _collect_defects(
            metrics(活性={"blink_count": 0}), silence_seconds=0,
            face_px=float("nan"))
            if d.code == "no_blink"]
        assert found and found[0].severity == "minor"


class TestIdentityStaysMajor:
    def test_identity_drift_not_downgraded(self):
        """身份漂移会训出平均脸且不可逆，不能跟着口型一起降级。"""
        found = [d for d in _collect_defects(
            metrics(一致性={"embed_drift_observed": -0.12, "duration_s": 10}),
            silence_seconds=0)
            if d.code == "identity_drift"]
        assert found and found[0].severity == "major"


def test_severity_values_are_known():
    everything = metrics(
        体态={"shoulder_tilt_range": 20.0, "hand_above_shoulder_frac": 0.5},
        协调性={"corr_best": 0.01, "lag_ms": 400.0, "silence_measured_s": 2.0},
        一致性={"embed_drift_observed": -0.3, "duration_s": 10},
        稳定性={"jump_times_s": [3.0], "luma_drift_observed": 30.0},
        合理性={"face_detect_rate": 0.5},
        活性={"blink_count": 0, "silence_jaw_max": 0.9,
             "silence_open_vs_speech_ratio": 0.99},
    )
    for defect in _collect_defects(everything, silence_seconds=2.0):
        assert defect.severity in SEVERITY_ORDER
