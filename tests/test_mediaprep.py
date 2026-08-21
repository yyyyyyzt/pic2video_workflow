#!/usr/bin/env python3
"""ffmpeg 封装。全部用 lavfi 合成素材，不联网不花钱。"""

from __future__ import annotations

import pytest

from compliance import ComplianceError, head_loudness, probe
from mediaprep import (audio_duration, loop_boomerang, mux_audio, normalize,
                       prepend_silence, replace_silent_head, snapshot_frames)

pytestmark = pytest.mark.ffmpeg


class TestPrependSilence:
    def test_extends_duration(self, media, tmp_path):
        src = media / "speech.mp3"
        out = prepend_silence(src, tmp_path / "p.mp3", 2.0)
        assert audio_duration(out) == pytest.approx(audio_duration(src) + 2.0, abs=0.2)

    def test_head_is_quiet(self, media, tmp_path):
        out = prepend_silence(media / "speech.mp3", tmp_path / "p.mp3", 2.0)
        assert head_loudness(out, 1.5) < -45


class TestNormalize:
    def test_fixes_ratio_and_resolution(self, media, tmp_path):
        out = normalize(media / "bad.mp4", tmp_path / "n.mp4",
                        profile="tencent-general")
        info = probe(out)
        assert min(info["width"], info["height"]) >= 1080
        assert info["width"] / info["height"] == pytest.approx(9 / 16, rel=0.02)

    def test_keeps_portrait_orientation(self, media, tmp_path):
        info = probe(normalize(media / "bad.mp4", tmp_path / "n.mp4"))
        assert info["height"] > info["width"]

    def test_keeps_landscape_orientation(self, media, tmp_path):
        info = probe(normalize(media / "silent.mp4", tmp_path / "n.mp4"))
        assert info["width"] > info["height"]

    def test_dimensions_are_even(self, media, tmp_path):
        info = probe(normalize(media / "bad.mp4", tmp_path / "n.mp4"))
        assert info["width"] % 2 == 0 and info["height"] % 2 == 0

    def test_preserves_audio(self, media, tmp_path):
        assert probe(normalize(media / "bad.mp4", tmp_path / "n.mp4"))["has_audio"]

    def test_no_audio_stays_no_audio(self, media, tmp_path):
        assert not probe(normalize(media / "silent.mp4", tmp_path / "n.mp4"))["has_audio"]

    def test_does_not_crop_duration(self, media, tmp_path):
        before = probe(media / "bad.mp4")["duration"]
        after = probe(normalize(media / "bad.mp4", tmp_path / "n.mp4"))["duration"]
        assert after == pytest.approx(before, abs=0.3)

    def test_hifi_profile_targets_4k(self, media, tmp_path):
        info = probe(normalize(media / "bad.mp4", tmp_path / "n.mp4",
                               profile="tencent-hifi"))
        assert min(info["width"], info["height"]) >= 2160

    def test_unknown_profile_raises(self, media, tmp_path):
        with pytest.raises(ComplianceError):
            normalize(media / "bad.mp4", tmp_path / "n.mp4", profile="没这个档")


class TestReplaceSilentHead:
    def test_keeps_duration_and_geometry(self, media, tmp_path):
        src = normalize(media / "bad.mp4", tmp_path / "n.mp4")
        before = probe(src)
        out = replace_silent_head(src, media / "face.png", tmp_path / "s.mp4", 2.0)
        after = probe(out)
        assert after["duration"] == pytest.approx(before["duration"], abs=0.3)
        assert (after["width"], after["height"]) == (before["width"], before["height"])
        assert after["fps"] == pytest.approx(before["fps"], abs=0.5)

    def test_keeps_audio(self, media, tmp_path):
        src = normalize(media / "bad.mp4", tmp_path / "n.mp4")
        out = replace_silent_head(src, media / "face.png", tmp_path / "s.mp4", 2.0)
        assert probe(out)["has_audio"]

    def test_rejects_video_shorter_than_head(self, media, tmp_path):
        with pytest.raises(ComplianceError, match="不够"):
            replace_silent_head(media / "silent.mp4", media / "face.png",
                                tmp_path / "s.mp4", 20.0)

    def test_missing_image_raises(self, media, tmp_path):
        with pytest.raises(ComplianceError, match="角色图"):
            replace_silent_head(media / "bad.mp4", tmp_path / "nope.png",
                                tmp_path / "s.mp4", 2.0)


class TestSnapshotFrames:
    def test_default_timestamps(self, media, tmp_path):
        frames = snapshot_frames(media / "bad.mp4", tmp_path / "th")
        assert len(frames) == 4
        assert all(p.is_file() and p.stat().st_size > 0 for p in frames)

    def test_skips_times_past_end(self, media, tmp_path):
        # silent.mp4 只有 4 秒，t=... 超出的会被跳过
        frames = snapshot_frames(media / "silent.mp4", tmp_path / "th",
                                 times=(0.5, 2.0, 99.0))
        assert len(frames) == 2

    def test_custom_times(self, media, tmp_path):
        frames = snapshot_frames(media / "bad.mp4", tmp_path / "th", times=(1.0,))
        assert [p.name for p in frames] == ["t1s.jpg"]


class TestLoopBoomerang:
    def test_reaches_target_duration(self, media, tmp_path):
        out = loop_boomerang(media / "silent.mp4", tmp_path / "l.mp4", 10.0)
        assert probe(out)["duration"] == pytest.approx(10.0, abs=0.5)

    def test_drops_audio(self, media, tmp_path):
        out = loop_boomerang(media / "bad.mp4", tmp_path / "l.mp4", 6.0)
        assert not probe(out)["has_audio"]


class TestMuxAudio:
    def test_attaches_audio_track(self, media, tmp_path):
        out = mux_audio(media / "silent.mp4", media / "speech.mp3", tmp_path / "m.mp4")
        assert probe(out)["has_audio"]

    def test_truncates_to_shortest(self, media, tmp_path):
        out = mux_audio(media / "silent.mp4", media / "speech.mp3", tmp_path / "m.mp4")
        assert probe(out)["duration"] == pytest.approx(4.0, abs=0.4)


def test_audio_duration_rejects_missing(tmp_path):
    with pytest.raises(ComplianceError):
        audio_duration(tmp_path / "nope.mp3")
