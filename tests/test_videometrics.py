#!/usr/bin/env python3
"""客观指标。

绝大多数测试**不需要视频**：把 FaceTrack 里那几条时间序列直接构造出来，
就能验证指标的数学。真视频只用来做一次端到端冒烟。
这样做还有个好处——可以造出「理想素材」和「已知有某个缺陷的素材」，
验证指标真的能把两者分开，而不是只验证它不崩。
"""

from __future__ import annotations

import numpy as np
import pytest

from videometrics.aliveness import aliveness_metrics, count_blinks
from videometrics.facetrack import FaceTrack
from videometrics.identity import cosine_similarity, drift_slope
from videometrics.lipsync import (align_lag, detect_silence_head, lipsync_metrics,
                                  onset_index)
from videometrics.plausibility import longest_false_run, outlier_fraction
from videometrics.report import THRESHOLDS, _collect_defects
from videometrics.stability import find_freezes, find_spikes, jitter


def make_track(n: int = 200, fps: float = 25.0, **overrides) -> FaceTrack:
    """构造一条「完美」轨迹，然后按需覆盖某几列来注入缺陷。

    体态那几列默认全 NaN，等于「没有 pose 数据」，这样人脸相关的测试
    不会被体态判定干扰；要测体态就显式传进来（见 test_anatomy.py）。
    """
    zeros = np.zeros(n)
    nans = np.full(n, np.nan)
    fields = dict(
        fps=fps, frame_count=n, width=1080, height=1920,
        detected=np.ones(n, dtype=bool),
        mouth_open=zeros + 0.03,
        mouth_width=zeros + 0.5,
        blink=zeros,
        jaw_open=zeros,
        face_size=zeros + 0.45,
        face_cx=zeros + 0.5,
        face_cy=zeros + 0.5,
        yaw_proxy=zeros,
        frame_mean=zeros + 128.0,
        frame_diff=zeros + 1.0,
        mouth_sharpness=zeros + 90.0,
        face_px=zeros + 500.0,
        shoulder_l_x=nans.copy(),
        shoulder_l_y=nans.copy(),
        shoulder_r_x=nans.copy(),
        shoulder_r_y=nans.copy(),
        neck_edge_ratio=nans.copy(),
        wrist_above_shoulder=nans.copy(),
        embeddings=[],
    )
    fields.update(overrides)
    return FaceTrack(**fields)


# ---------------------------------------------------------------------------
# 口型同步
# ---------------------------------------------------------------------------

class TestAlignLag:
    def test_identical_signals_have_zero_lag(self):
        x = np.sin(np.linspace(0, 12, 200))
        lag, corr, corr0 = align_lag(x, x, 20)
        assert lag == 0
        assert corr == pytest.approx(1.0, abs=1e-6)
        assert corr0 == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.parametrize("shift", [3, 7, -5, -11])
    def test_recovers_known_shift(self, shift):
        """核心保证：注入已知的时间偏移，估计器必须还原出来。"""
        base = np.sin(np.linspace(0, 20, 300))
        if shift >= 0:
            signal, reference = base, np.roll(base, -shift)
        else:
            signal, reference = np.roll(base, shift), base
        lag, corr, _ = align_lag(signal, reference, 25)
        assert lag == pytest.approx(shift, abs=1)
        assert corr > 0.95

    def test_uncorrelated_noise_gives_low_corr(self):
        rng = np.random.default_rng(0)
        _, corr, _ = align_lag(rng.normal(size=400), rng.normal(size=400), 20)
        assert abs(corr) < 0.4

    def test_constant_signal_is_not_nan(self):
        lag, corr, _ = align_lag(np.ones(50), np.sin(np.linspace(0, 9, 50)), 10)
        assert lag == 0 and corr == pytest.approx(0.0)

    def test_too_short_returns_nan(self):
        _, corr, _ = align_lag(np.ones(2), np.ones(2), 5)
        assert np.isnan(corr)

    def test_handles_nan_in_input(self):
        x = np.sin(np.linspace(0, 12, 200))
        y = x.copy()
        y[::10] = np.nan
        _, corr, _ = align_lag(y, x, 10)
        assert corr > 0.8


class TestOnsetIndex:
    def test_finds_step(self):
        signal = np.concatenate([np.zeros(50), np.ones(50)])
        assert onset_index(signal) == pytest.approx(50, abs=1)

    def test_ignores_single_frame_spike(self):
        signal = np.zeros(100)
        signal[10] = 1.0                       # 一帧毛刺，不算起始
        signal[60:] = 1.0
        assert onset_index(signal) >= 55

    def test_flat_signal_returns_minus_one(self):
        assert onset_index(np.ones(100)) == -1

    def test_too_short_returns_minus_one(self):
        assert onset_index(np.array([1.0])) == -1


class TestDetectSilenceHead:
    def test_measures_leading_quiet(self):
        env = np.concatenate([np.zeros(50), np.full(100, 0.2)])
        assert detect_silence_head(env, fps=25.0) == pytest.approx(2.0, abs=0.05)

    def test_no_silence_returns_zero(self):
        assert detect_silence_head(np.full(100, 0.2), fps=25.0) == 0.0

    def test_all_silent(self):
        assert detect_silence_head(np.zeros(50), fps=25.0) == pytest.approx(2.0, abs=0.05)

    def test_empty(self):
        assert detect_silence_head(np.zeros(0), fps=25.0) == 0.0

    def test_insensitive_to_overall_volume(self):
        quiet = np.concatenate([np.zeros(25), np.full(75, 0.01)])
        loud = np.concatenate([np.zeros(25), np.full(75, 0.9)])
        assert detect_silence_head(quiet, fps=25.0) == detect_silence_head(loud, fps=25.0)


class TestLipsyncMetrics:
    def test_reports_unreliable_lag_when_uncorrelated(self):
        rng = np.random.default_rng(1)
        track = make_track(n=200, mouth_open=rng.normal(0.05, 0.02, 200))
        out = lipsync_metrics(track, rng.normal(0.1, 0.03, 200))
        assert out["lag_reliable"] is False
        assert "lag_ms" not in out
        assert "lag_note" in out

    def test_reports_lag_when_correlated(self):
        env = np.abs(np.sin(np.linspace(0, 20, 250))) * 0.2
        track = make_track(n=250, mouth_open=np.roll(env, 4) * 0.5)
        out = lipsync_metrics(track, env)
        assert out["lag_reliable"] is True
        assert out["lag_ms"] == pytest.approx(4 * 1000 / 25.0, abs=45)

    def test_flags_declared_silence_mismatch(self):
        env = np.full(200, 0.2)               # 全程有声，没有静默头
        track = make_track(n=200)
        out = lipsync_metrics(track, env, silence_seconds=2.0)
        assert out["silence_mismatch"] is True
        assert out["silence_measured_s"] == pytest.approx(0.0, abs=0.1)

    def test_no_mismatch_when_declared_matches(self):
        env = np.concatenate([np.zeros(50), np.full(150, 0.2)])
        out = lipsync_metrics(make_track(n=200), env, silence_seconds=2.0)
        assert "silence_mismatch" not in out

    def test_short_clip_bails_out(self):
        out = lipsync_metrics(make_track(n=4), np.zeros(4))
        assert "note" in out

    def test_no_audio(self):
        out = lipsync_metrics(make_track(n=200), np.zeros(0))
        assert out["has_audio"] is False


# ---------------------------------------------------------------------------
# 活性
# ---------------------------------------------------------------------------

class TestCountBlinks:
    def test_counts_rising_edges_not_frames(self):
        blink = np.zeros(100)
        blink[10:14] = 0.9        # 一次眨眼持续 4 帧
        blink[50:53] = 0.9
        assert count_blinks(blink) == 2

    def test_no_blink(self):
        assert count_blinks(np.zeros(100)) == 0

    def test_below_threshold_ignored(self):
        assert count_blinks(np.full(100, 0.3)) == 0

    def test_always_closed_counts_once(self):
        assert count_blinks(np.full(100, 0.9)) == 1

    def test_nan_safe(self):
        blink = np.full(50, np.nan)
        blink[10:13] = 0.9
        assert count_blinks(blink) == 1


class TestAliveness:
    def test_detects_no_blink(self):
        out = aliveness_metrics(make_track(n=250))
        assert out["blink_count"] == 0

    def test_skips_rate_judgement_on_short_clip(self):
        out = aliveness_metrics(make_track(n=250))       # 10 秒
        assert "blink_rate_natural" not in out
        assert "blink_rate_note" in out

    def test_judges_rate_on_long_clip(self):
        blink = np.zeros(1500)                            # 60 秒 @25fps
        for i in range(0, 1500, 100):                     # 每 4 秒眨一次 = 15/min
            blink[i:i + 3] = 0.9
        out = aliveness_metrics(make_track(n=1500, blink=blink))
        assert out["blink_rate_natural"] is True

    def test_detects_frozen_silence(self):
        track = make_track(n=250, frame_diff=np.concatenate(
            [np.zeros(50), np.ones(200)]))
        out = aliveness_metrics(track, silence_seconds=2.0)
        assert out["silence_frozen"] is True

    def test_live_silence_not_flagged_frozen(self):
        out = aliveness_metrics(make_track(n=250), silence_seconds=2.0)
        assert out["silence_frozen"] is False

    def test_detects_mouth_held_open_during_silence(self):
        """嘴张着不动：静默段标准差接近 0，只看 std 会漏判，要靠开口幅度比。"""
        rng = np.random.default_rng(7)
        mouth = np.concatenate([np.full(50, 0.10),                    # 张着不动
                                np.abs(rng.normal(0.06, 0.03, 200))])  # 正常说话
        out = aliveness_metrics(make_track(n=250, mouth_open=mouth),
                                silence_seconds=2.0)
        assert out["silence_vs_speech_mouth_ratio"] < 0.05      # std 之比看不出问题
        assert out["silence_open_vs_speech_ratio"] > 0.8        # 开口幅度比抓到了

    def test_closed_mouth_during_silence_scores_low(self):
        rng = np.random.default_rng(2)
        mouth = np.concatenate([np.full(50, 0.005),
                                np.abs(rng.normal(0.08, 0.03, 200))])
        out = aliveness_metrics(make_track(n=250, mouth_open=mouth),
                                silence_seconds=2.0)
        assert out["silence_open_vs_speech_ratio"] < 0.3

    def test_no_silence_skips_silence_block(self):
        out = aliveness_metrics(make_track(n=250), silence_seconds=0.0)
        assert "silence_frames" not in out


# ---------------------------------------------------------------------------
# 一致性 / 稳定性 / 合理性
# ---------------------------------------------------------------------------

class TestIdentityHelpers:
    def test_cosine_of_identical(self):
        v = np.array([1.0, 2.0, 3.0])
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_cosine_of_orthogonal(self):
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_cosine_zero_vector_is_nan(self):
        assert np.isnan(cosine_similarity([0, 0], [1, 1]))

    def test_drift_slope_detects_decline(self):
        assert drift_slope(np.linspace(1.0, 0.5, 100)) < 0

    def test_drift_slope_flat_on_noise(self):
        rng = np.random.default_rng(3)
        assert abs(drift_slope(rng.normal(0.9, 0.02, 500))) < 1e-3

    def test_drift_slope_too_short(self):
        assert np.isnan(drift_slope(np.array([1.0, 0.9])))


class TestStability:
    def test_jitter_zero_on_static_face(self):
        track = make_track()
        out = jitter(track.face_cx, track.face_cy)
        assert out["accel_p99"] == pytest.approx(0.0)

    def test_jitter_ignores_constant_velocity(self):
        # 匀速平移是正常的身体移动，不该算抖
        n = 200
        out = jitter(np.linspace(0.4, 0.6, n), np.full(n, 0.5))
        assert out["accel_max"] < 1e-6

    def test_jitter_catches_shake(self):
        n = 200
        shake = 0.5 + 0.01 * (-1) ** np.arange(n)
        out = jitter(shake, np.full(n, 0.5))
        assert out["accel_p99"] > 0.01

    def test_find_spikes_locates_jump(self):
        diff = np.full(100, 1.0)
        diff[42] = 60.0
        assert find_spikes(diff) == [42]

    def test_find_spikes_quiet_on_uniform(self):
        assert find_spikes(np.full(100, 1.0)) == []

    def test_find_spikes_needs_enough_frames(self):
        assert find_spikes(np.array([1.0, 99.0])) == []

    def test_find_freezes_locates_run(self):
        diff = np.full(100, 1.0)
        diff[20:30] = 0.0
        assert find_freezes(diff) == [(20, 10)]

    def test_find_freezes_ignores_short_run(self):
        diff = np.full(100, 1.0)
        diff[20:22] = 0.0
        assert find_freezes(diff) == []

    def test_find_freezes_handles_trailing_run(self):
        diff = np.concatenate([np.ones(90), np.zeros(10)])
        assert find_freezes(diff) == [(90, 10)]


class TestPlausibility:
    def test_longest_false_run(self):
        flags = np.array([1, 1, 0, 0, 0, 1, 0], dtype=bool)
        assert longest_false_run(flags) == 3

    def test_longest_false_run_all_true(self):
        assert longest_false_run(np.ones(10, dtype=bool)) == 0

    def test_outlier_fraction_zero_on_constant(self):
        assert outlier_fraction(np.full(50, 0.5)) == 0.0

    def test_outlier_fraction_catches_outliers(self):
        values = np.full(100, 0.5)
        values[:5] = 5.0
        assert outlier_fraction(values) == pytest.approx(0.05, abs=0.02)

    def test_outlier_fraction_needs_samples(self):
        assert np.isnan(outlier_fraction(np.array([1.0, 2.0])))


# ---------------------------------------------------------------------------
# 缺陷判定
# ---------------------------------------------------------------------------

class TestCollectDefects:
    def _metrics(self, **blocks):
        base = {"协调性": {}, "一致性": {}, "稳定性": {}, "合理性": {}, "活性": {}}
        base.update(blocks)
        return base

    def test_clean_metrics_yield_no_defects(self):
        metrics = self._metrics(
            协调性={"corr_best": 0.7, "silence_measured_s": 2.0},
            活性={"blink_count": 3, "silence_blink_count": 1,
                 "silence_vs_speech_mouth_ratio": 0.2,
                 "silence_open_vs_speech_ratio": 0.2,
                 "silence_jaw_max": 0.01},
            合理性={"face_detect_rate": 1.0},
        )
        assert _collect_defects(metrics, silence_seconds=2.0) == []

    def test_weak_lipsync_is_major(self):
        metrics = self._metrics(协调性={"corr_best": 0.05})
        codes = [d.code for d in _collect_defects(metrics, silence_seconds=0)]
        assert "lipsync_weak" in codes

    def test_no_blink_is_minor(self):
        """降级依据：静默段不参与训练，口播段不眨眼影响观感但不致命。
        分级策略的完整断言见 test_severity_policy.py。"""
        metrics = self._metrics(活性={"blink_count": 0})
        found = [d for d in _collect_defects(metrics, silence_seconds=0)
                 if d.code == "no_blink"]
        assert found and found[0].severity == "minor"

    def test_mouth_not_closed_during_silence(self):
        metrics = self._metrics(
            协调性={"silence_measured_s": 2.0},
            活性={"blink_count": 2, "silence_open_vs_speech_ratio": 0.98,
                 "silence_blink_count": 1})
        codes = [d.code for d in _collect_defects(metrics, silence_seconds=2.0)]
        assert "silence_mouth_not_closed" in codes

    def test_silence_defects_skipped_without_measured_silence(self):
        """声称有静默但实测没有时，不该报静默段的缺陷。"""
        metrics = self._metrics(
            协调性={"silence_measured_s": 0.0},
            活性={"blink_count": 2, "silence_open_vs_speech_ratio": 0.98})
        codes = [d.code for d in _collect_defects(metrics, silence_seconds=0.0)]
        assert "silence_mouth_not_closed" not in codes

    def test_identity_drift_only_when_declining(self):
        rising = self._metrics(一致性={"embed_drift_observed": +0.2, "duration_s": 10})
        assert not [d for d in _collect_defects(rising, silence_seconds=0)
                    if d.code == "identity_drift"]
        falling = self._metrics(一致性={"embed_drift_observed": -0.2, "duration_s": 10})
        assert [d for d in _collect_defects(falling, silence_seconds=0)
                if d.code == "identity_drift"]

    def test_thresholds_are_ordered(self):
        t = THRESHOLDS
        assert t["lipsync_corr_major"] < t["lipsync_corr_minor"]
        assert t["lag_ms_minor"] < t["lag_ms_major"]
        assert t["identity_drift_minor"] < t["identity_drift_major"]
        assert t["detect_rate_major"] < t["detect_rate_minor"]


# ---------------------------------------------------------------------------
# 端到端（需要真实人脸视频）
# ---------------------------------------------------------------------------

@pytest.mark.metrics
@pytest.mark.realface
class TestEndToEnd:
    def test_evaluate_real_video(self, real_video):
        import videometrics

        result = videometrics.evaluate(real_video, silence_seconds=2.0)
        assert result.available, result.note
        assert set(result.metrics) == {"体态", "协调性", "一致性",
                                       "稳定性", "合理性", "活性"}
        assert result.metrics["合理性"]["face_detect_rate"] > 0.5
        assert result.metrics["合理性"]["face_px_median"] > 0
        assert result.segments

    def test_deterministic(self, real_video):
        """同一输入必须给同一结果，否则没法用来做回归和排序。"""
        import videometrics

        a = videometrics.evaluate_to_dict(real_video, silence_seconds=2.0)
        b = videometrics.evaluate_to_dict(real_video, silence_seconds=2.0)
        assert a["metrics"] == b["metrics"]
        assert a["defects"] == b["defects"]
