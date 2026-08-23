#!/usr/bin/env python3
"""体态指标。按实拍反馈这一维优先级最高，所以测得细一些。

和 test_videometrics 一样，靠构造时间序列来测，不需要真视频。
"""

from __future__ import annotations

import numpy as np
import pytest

from videometrics.anatomy import (NECK_EDGE_RISE_MAJOR, NECK_EDGE_RISE_MINOR,
                                  anatomy_metrics, line_angle, robust_range)
from videometrics.report import _collect_defects

from test_videometrics import make_track


def body_track(n: int = 200, **overrides):
    """一个「站得很稳」的身体：肩线水平、宽度恒定、手放下、颈部纹理平稳。"""
    zeros = np.zeros(n)
    defaults = dict(
        shoulder_l_x=zeros + 0.30, shoulder_l_y=zeros + 0.70,
        shoulder_r_x=zeros + 0.70, shoulder_r_y=zeros + 0.70,
        neck_edge_ratio=zeros + 0.5,
        wrist_above_shoulder=zeros,
    )
    defaults.update(overrides)
    return make_track(n=n, **defaults)


class TestLineAngle:
    def test_horizontal_is_zero(self):
        angle = line_angle(np.array([0.0]), np.array([0.5]),
                           np.array([1.0]), np.array([0.5]))
        assert angle[0] == pytest.approx(0.0)

    def test_forty_five_degrees(self):
        angle = line_angle(np.array([0.0]), np.array([0.0]),
                           np.array([1.0]), np.array([1.0]))
        assert angle[0] == pytest.approx(45.0)

    def test_sign_follows_direction(self):
        down = line_angle(np.array([0.0]), np.array([0.0]),
                          np.array([1.0]), np.array([1.0]))[0]
        up = line_angle(np.array([0.0]), np.array([1.0]),
                        np.array([1.0]), np.array([0.0]))[0]
        assert down > 0 > up


class TestRobustRange:
    def test_ignores_single_spike(self):
        values = np.full(200, 1.0)
        values[0] = 100.0
        assert robust_range(values) == pytest.approx(0.0, abs=0.01)

    def test_measures_real_spread(self):
        assert robust_range(np.linspace(0.0, 10.0, 200)) == pytest.approx(9.0, abs=0.3)

    def test_too_few_samples(self):
        assert np.isnan(robust_range(np.array([1.0, 2.0])))


class TestAnatomyMetrics:
    def test_reports_note_without_pose(self):
        out = anatomy_metrics(make_track(n=100))
        assert "note" in out
        assert "pose_detect_rate" not in out

    def test_detect_rate_accounts_for_stride(self):
        """抽帧算的 pose 不该被当成漏检：stride=5 时满检也应该是 100%。"""
        n = 100
        sampled = np.full(n, np.nan)
        sampled[::5] = 0.30
        track = body_track(n=n, shoulder_l_x=sampled)
        out = anatomy_metrics(track, pose_stride=5)
        assert out["pose_detect_rate"] == pytest.approx(1.0, abs=0.05)
        assert out["pose_frames_sampled"] == 20

    def test_stable_body_has_low_tilt(self):
        out = anatomy_metrics(body_track())
        assert out["shoulder_tilt_range"] == pytest.approx(0.0, abs=0.01)
        assert out["shoulder_width_cv"] == pytest.approx(0.0, abs=0.01)

    def test_detects_tilting_shoulders(self):
        n = 200
        wobble = 0.70 + 0.05 * np.sin(np.linspace(0, 12, n))
        out = anatomy_metrics(body_track(n=n, shoulder_r_y=wobble))
        assert out["shoulder_tilt_range"] > 8.0

    def test_detects_width_instability(self):
        n = 200
        breathing = 0.70 + 0.06 * np.sin(np.linspace(0, 20, n))
        out = anatomy_metrics(body_track(n=n, shoulder_r_x=breathing))
        assert out["shoulder_width_cv"] > 0.08

    def test_shoulder_head_ratio_drift(self):
        n = 200
        # 肩膀慢慢变宽而头不变 = 体型被改
        widening = np.linspace(0.70, 0.95, n)
        out = anatomy_metrics(body_track(n=n, shoulder_r_x=widening))
        assert out["shoulder_head_ratio_drift"] > 0.2

    def test_neck_rise_flat_when_stable(self):
        out = anatomy_metrics(body_track())
        assert out["neck_edge_rise"] == pytest.approx(1.0, abs=0.05)

    def test_neck_rise_catches_late_artifact(self):
        n = 200
        neck = np.full(n, 0.4)
        neck[150:] = 2.0                       # 后段颈部突然长出结构
        out = anatomy_metrics(body_track(n=n, neck_edge_ratio=neck))
        assert out["neck_edge_rise"] > NECK_EDGE_RISE_MAJOR
        assert out["neck_edge_worst_at_s"] >= 150 / 25.0 - 0.1

    def test_hand_above_shoulder_fraction(self):
        n = 200
        wrist = np.zeros(n)
        wrist[:40] = 1.0
        out = anatomy_metrics(body_track(n=n, wrist_above_shoulder=wrist))
        assert out["hand_above_shoulder_frac"] == pytest.approx(0.2, abs=0.01)


class TestAnatomyDefects:
    def _metrics(self, body):
        return {"体态": body, "协调性": {}, "一致性": {},
                "稳定性": {}, "合理性": {}, "活性": {"blink_count": 3}}

    def test_clean_body_no_defects(self):
        body = anatomy_metrics(body_track())
        defects = _collect_defects(self._metrics(body), silence_seconds=0)
        assert [d for d in defects if d.dimension == "体态"] == []

    def test_neck_artifact_is_major(self):
        neck = np.full(200, 0.4)
        neck[150:] = 2.0
        body = anatomy_metrics(body_track(n=200, neck_edge_ratio=neck))
        found = [d for d in _collect_defects(self._metrics(body), silence_seconds=0)
                 if d.code == "neck_bone_artifact"]
        assert found and found[0].severity == "major"
        assert found[0].at_seconds is not None

    def test_neck_minor_band(self):
        body = {"neck_edge_rise": (NECK_EDGE_RISE_MINOR + NECK_EDGE_RISE_MAJOR) / 2,
                "neck_edge_ratio_p90": 1.0}
        found = [d for d in _collect_defects(self._metrics(body), silence_seconds=0)
                 if d.code == "neck_bone_artifact"]
        assert found and found[0].severity == "minor"

    def test_shoulder_instability_is_major(self):
        body = {"shoulder_tilt_range": 20.0}
        found = [d for d in _collect_defects(self._metrics(body), silence_seconds=0)
                 if d.code == "shoulder_unstable"]
        assert found and found[0].severity == "major"

    def test_hand_too_high_is_major(self):
        body = {"hand_above_shoulder_frac": 0.4}
        found = [d for d in _collect_defects(self._metrics(body), silence_seconds=0)
                 if d.code == "hand_too_high"]
        assert found and found[0].severity == "major"
