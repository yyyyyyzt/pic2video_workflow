#!/usr/bin/env python3
"""wsclient 里不联网的部分：.env 解析和成本校准表。

成本校准是加权平均，权重搞错会让 plan 的估算长期偏高——
实测同一模型 3 秒算出 $0.053/秒、7 秒算出 $0.040/秒，简单平均会偏向短任务。
"""

from __future__ import annotations

import json

import pytest

import wsclient
from tts import CHARS_PER_SECOND, TTSError, estimate_seconds, read_script, resolve_voice


class TestLoadDotenv:
    def test_reads_pairs(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n", encoding="utf-8")
        monkeypatch.delenv("FOO", raising=False)
        wsclient.load_dotenv(str(env))
        import os
        assert os.environ["FOO"] == "bar"

    def test_does_not_override_existing(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("FOO=fromfile\n", encoding="utf-8")
        monkeypatch.setenv("FOO", "fromenv")
        wsclient.load_dotenv(str(env))
        import os
        assert os.environ["FOO"] == "fromenv"

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# c\n\nA=1\nnoequals\n", encoding="utf-8")
        monkeypatch.delenv("A", raising=False)
        wsclient.load_dotenv(str(env))
        import os
        assert os.environ["A"] == "1"

    def test_strips_quotes(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text('K="quoted"\n', encoding="utf-8")
        monkeypatch.delenv("K", raising=False)
        wsclient.load_dotenv(str(env))
        import os
        assert os.environ["K"] == "quoted"

    def test_missing_file_is_noop(self, tmp_path):
        wsclient.load_dotenv(str(tmp_path / "nope"))


class TestCalibration:
    def test_missing_file_returns_empty(self, tmp_path):
        assert wsclient.load_calibration(tmp_path / "nope.json") == {}

    def test_corrupt_json_returns_empty(self, tmp_path):
        bad = tmp_path / "c.json"
        bad.write_text("{not json", encoding="utf-8")
        assert wsclient.load_calibration(bad) == {}

    def test_records_per_second(self, tmp_path):
        path = tmp_path / "c.json"
        wsclient.record_calibration("m", 0.40, 10.0, path)
        entry = wsclient.load_calibration(path)["m"]
        assert entry["per_second"] == pytest.approx(0.04)
        assert entry["samples"] == 1

    def test_weights_by_duration_not_sample_count(self, tmp_path):
        """长任务权重更大：3 秒那次的高单价不该把整体拉高太多。"""
        path = tmp_path / "c.json"
        wsclient.record_calibration("m", 0.16, 3.0, path)     # 0.053/s
        wsclient.record_calibration("m", 2.80, 70.0, path)    # 0.040/s
        per_second = wsclient.load_calibration(path)["m"]["per_second"]
        assert per_second == pytest.approx((0.16 + 2.80) / 73.0, rel=1e-3)
        assert per_second < 0.042            # 简单平均会得到 0.0465

    def test_ignores_nonpositive(self, tmp_path):
        path = tmp_path / "c.json"
        wsclient.record_calibration("m", 0.0, 10.0, path)
        wsclient.record_calibration("m", 0.4, 0.0, path)
        assert wsclient.load_calibration(path) == {}

    def test_upgrades_legacy_entry(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"m": {"per_second": 0.05, "samples": 1}}),
                        encoding="utf-8")
        wsclient.record_calibration("m", 0.40, 10.0, path)
        entry = wsclient.load_calibration(path)["m"]
        assert entry["total_seconds"] == pytest.approx(10.0)
        assert entry["samples"] == 2


class TestTTSHelpers:
    def test_estimate_ignores_whitespace(self):
        assert estimate_seconds("一二三四五") == estimate_seconds("一二三\n四五 ")

    def test_estimate_uses_declared_rate(self):
        text = "字" * int(CHARS_PER_SECOND * 10)
        assert estimate_seconds(text) == pytest.approx(10.0, abs=0.2)

    def test_faster_speech_is_shorter(self):
        text = "字" * 100
        assert estimate_seconds(text, 2.0) < estimate_seconds(text, 1.0)

    def test_zero_speed_does_not_divide_by_zero(self):
        assert estimate_seconds("字" * 10, 0.0) > 0

    def test_read_script_strips_comments(self, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text("# 注释\n\n正文一\n正文二\n", encoding="utf-8")
        assert read_script(f) == "正文一\n正文二"

    def test_read_script_all_comments_raises(self, tmp_path):
        f = tmp_path / "s.txt"
        f.write_text("# 全是注释\n\n", encoding="utf-8")
        with pytest.raises(TTSError):
            read_script(f)

    def test_resolve_voice_by_full_key(self):
        assert resolve_voice("qwen:Cherry").voice_id == "Cherry"

    def test_resolve_voice_by_bare_id(self):
        assert resolve_voice("felix_zh").key == "seed:felix_zh"

    def test_resolve_voice_unknown(self):
        with pytest.raises(TTSError):
            resolve_voice("没这个音色")


def test_api_key_error_mentions_env_var(monkeypatch):
    monkeypatch.delenv("WAVESPEED_API_KEY", raising=False)
    with pytest.raises(wsclient.WaveSpeedError, match="WAVESPEED_API_KEY"):
        wsclient.api_key()
