#!/usr/bin/env python3
"""入库校验。重点是各档位的边界和「开头静默」的检测窗口——
窗口写死 2 秒曾经让 1 秒静默的素材被误判不合格。"""

from __future__ import annotations

import pytest

import compliance
from compliance import PROFILES, ComplianceError, _ratio_name, check, probe


class TestRatioName:
    def test_exact_landscape(self):
        assert _ratio_name(1920, 1080) == "16:9"

    def test_exact_portrait(self):
        assert _ratio_name(1080, 1920) == "9:16"

    def test_within_tolerance(self):
        assert _ratio_name(1918, 1080) == "16:9"

    def test_480x832_is_not_9_16(self):
        # 生成模型最常见的输出尺寸，比例 1.733 而不是 1.778，平台会退
        assert _ratio_name(480, 832) != "9:16"

    def test_falls_back_to_reduced_fraction(self):
        assert _ratio_name(480, 832) == "15:26"

    def test_square(self):
        assert _ratio_name(512, 512) == "1:1"


def _fake_probe(monkeypatch, **overrides):
    info = {"width": 1080, "height": 1920, "fps": 30.0, "duration": 90.0,
            "size_bytes": 50 * 1024 ** 2, "container": "mp4", "vcodec": "h264",
            "pix_fmt": "yuv420p", "has_audio": True, "acodec": "aac"}
    info.update(overrides)
    monkeypatch.setattr(compliance, "probe", lambda _p: info)
    monkeypatch.setattr(compliance, "head_loudness", lambda _p, _s=2.0: -90.0)
    return info


def _result(report, name):
    return next(c for c in report.checks if c.name == name)


class TestCheckBoundaries:
    def test_duration_lower_bound(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, duration=59.9)
        assert not _result(check(f, "tencent-general"), "时长").ok
        _fake_probe(monkeypatch, duration=60.0)
        assert _result(check(f, "tencent-general"), "时长").ok

    def test_duration_upper_bound(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, duration=600.0)
        assert _result(check(f, "tencent-general"), "时长").ok
        _fake_probe(monkeypatch, duration=600.1)
        assert not _result(check(f, "tencent-general"), "时长").ok

    def test_short_edge_boundary(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, width=1079, height=1918)
        assert not _result(check(f, "tencent-general"), "分辨率").ok
        _fake_probe(monkeypatch, width=1080, height=1920)
        assert _result(check(f, "tencent-general"), "分辨率").ok

    def test_fps_boundaries(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        for fps, ok in ((24.9, False), (25.0, True), (60.0, True), (60.1, False)):
            _fake_probe(monkeypatch, fps=fps)
            assert _result(check(f, "tencent-general"), "帧率").ok is ok

    def test_hifi_requires_4k(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, width=2160, height=3840, duration=200)
        assert _result(check(f, "tencent-hifi"), "分辨率").ok
        _fake_probe(monkeypatch, width=1080, height=1920, duration=200)
        assert not _result(check(f, "tencent-hifi"), "分辨率").ok

    def test_aliyun_needs_30fps(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, fps=25.0, duration=30, has_audio=False)
        assert not _result(check(f, "aliyun-fewshot"), "帧率").ok

    def test_unknown_container(self, monkeypatch, tmp_path):
        f = tmp_path / "v.avi"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, container="avi")
        assert not _result(check(f, "tencent-general"), "封装格式").ok

    def test_unknown_profile_raises(self, tmp_path):
        with pytest.raises(ComplianceError):
            check(tmp_path / "v.mp4", "没这个档位")


class TestSilentHeadWindow:
    """回归测试：窗口必须跟着实际静默秒数走。"""

    def test_window_defaults_to_profile_minimum(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch)
        seen = []
        monkeypatch.setattr(compliance, "head_loudness",
                            lambda _p, s=2.0: seen.append(s) or -90.0)
        check(f, "tencent-general")
        assert seen == [PROFILES["tencent-general"].min_silent_head]

    def test_window_follows_declared_silence(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch)
        seen = []
        monkeypatch.setattr(compliance, "head_loudness",
                            lambda _p, s=2.0: seen.append(s) or -90.0)
        check(f, "tencent-general", silence_seconds=1.0)
        assert seen == [1.0]

    def test_one_second_silence_passes_when_that_second_is_quiet(self, monkeypatch, tmp_path):
        """核心回归：只有 1 秒静默时，不应该去测第 2 秒（那里已经在说话）。"""
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch)
        # 前 1 秒安静，窗口一旦超过 1 秒就会读到口播
        monkeypatch.setattr(compliance, "head_loudness",
                            lambda _p, s=2.0: -90.0 if s <= 1.0 else -20.0)
        assert _result(check(f, "tencent-general", silence_seconds=1.0), "开头静默").ok
        assert not _result(check(f, "tencent-general", silence_seconds=2.0), "开头静默").ok

    def test_window_clamped_to_duration(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch, duration=0.8)
        seen = []
        monkeypatch.setattr(compliance, "head_loudness",
                            lambda _p, s=2.0: seen.append(s) or -90.0)
        check(f, "tencent-general", silence_seconds=3.0)
        assert seen[0] <= 0.8

    def test_window_never_below_floor(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch)
        seen = []
        monkeypatch.setattr(compliance, "head_loudness",
                            lambda _p, s=2.0: seen.append(s) or -90.0)
        check(f, "tencent-general", silence_seconds=0.01)
        assert seen[0] == compliance.SILENT_HEAD_MIN_WINDOW

    def test_missing_audio_fails(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        _fake_probe(monkeypatch)
        monkeypatch.setattr(compliance, "head_loudness", lambda _p, _s=2.0: None)
        assert not _result(check(f, "tencent-general"), "开头静默").ok

    def test_aliyun_hifi_wants_15s(self):
        assert PROFILES["aliyun-hifi"].min_silent_head == 15.0


@pytest.mark.ffmpeg
class TestProbeReal:
    def test_reads_dimensions_and_audio(self, media):
        info = probe(media / "bad.mp4")
        assert (info["width"], info["height"]) == (480, 832)
        assert info["fps"] == pytest.approx(25.0, abs=0.1)
        assert info["has_audio"] is True
        assert info["duration"] == pytest.approx(12.0, abs=0.3)

    def test_detects_missing_audio(self, media):
        assert probe(media / "silent.mp4")["has_audio"] is False

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ComplianceError):
            probe(tmp_path / "nope.mp4")

    def test_non_video_raises(self, media):
        with pytest.raises(ComplianceError):
            probe(media / "speech.mp3")


@pytest.mark.ffmpeg
class TestHeadLoudnessReal:
    def test_silent_head_is_quiet(self, video_with_silent_head):
        from compliance import head_loudness
        assert head_loudness(video_with_silent_head, 1.5) < -45

    def test_speech_region_is_loud(self, video_with_silent_head):
        from compliance import head_loudness
        # 窗口拉到 6 秒就会把后面的正弦包进来
        assert head_loudness(video_with_silent_head, 6.0) > -45

    def test_no_audio_returns_none(self, media):
        from compliance import head_loudness
        assert head_loudness(media / "silent.mp4", 2.0) is None


@pytest.mark.ffmpeg
def test_bad_sample_fails_multiple_checks(media):
    report = check(media / "bad.mp4", "tencent-general")
    failed = {c.name for c in report.checks if not c.ok}
    assert {"时长", "分辨率", "宽高比"} <= failed
    assert not report.passed
