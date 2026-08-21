#!/usr/bin/env python3
"""汇总表和 HTML 对照页。HTML 只做结构性断言，不测样式。"""

from __future__ import annotations

import json

from lab.report import defect_lines, summarize, write_sweep_index
from lab.table import render_table, text_width
from matrices import expand, resolve_matrix


class TestTable:
    def test_cjk_counts_as_two_columns(self):
        assert text_width("中文") == 4
        assert text_width("ab") == 2

    def test_columns_align_with_mixed_width(self):
        out = render_table(["名字", "值"], [["中文abc", "1"], ["x", "22"]])
        lines = out.splitlines()
        assert len({len(line.rstrip()) for line in lines}) <= 3

    def test_handles_no_rows(self):
        assert "名字" in render_table(["名字"], [])


class TestSummarize:
    def test_marks_failed_records(self):
        out = summarize([{"cell_id": "c1", "status": "failed", "error": "超时"}])
        assert "failed" in out and "超时" in out

    def test_shows_defect_counts(self):
        out = summarize([{
            "cell_id": "c1", "status": "ok", "compliance_passed": True,
            "final_probe": {"width": 1080, "height": 1920, "duration": 11.0},
            "cost_actual": 0.48,
            "metrics": {"available": True, "major_count": 2, "minor_count": 1},
        }])
        assert "2重/1轻" in out

    def test_totals_cost(self):
        out = summarize([
            {"cell_id": "a", "status": "ok", "cost_actual": 0.5, "final_probe": {}},
            {"cell_id": "b", "status": "ok", "cost_actual": 0.25, "final_probe": {}},
        ])
        assert "0.750" in out

    def test_tolerates_missing_metrics(self):
        assert summarize([{"cell_id": "a", "status": "ok", "final_probe": {}}])


class TestDefectLines:
    def test_lists_defects_with_time(self):
        text = defect_lines([{
            "cell_id": "c1",
            "metrics": {"defects": [
                {"dimension": "稳定性", "code": "frame_jump", "severity": "major",
                 "detail": "疑似跳帧", "at_seconds": 13.5}]},
        }])
        assert "frame_jump" in text and "13.5" in text

    def test_empty_when_no_defects(self):
        assert defect_lines([{"cell_id": "c1", "metrics": {"defects": []}}]).strip() == ""


class TestWriteSweepIndex:
    def _records(self, cells, video):
        return [{
            "cell_id": cells[0].id, "status": "ok",
            "final": str(video), "thumbs": [],
            "cost_actual": 0.48,
            "final_probe": {"width": 1080, "height": 1920, "duration": 11.0},
            "metrics": {"available": True, "major_count": 1, "minor_count": 0,
                        "metrics": {"协调性": {"corr_best": 0.42},
                                    "活性": {"blink_count": 3}},
                        "defects": [{"dimension": "协调性", "code": "lipsync_lag",
                                     "severity": "major", "detail": "偏移 200ms",
                                     "at_seconds": None}]},
        }]

    def test_writes_file_with_both_tabs(self, tmp_path):
        mx = resolve_matrix("screen10-models")
        cells = expand(mx)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        path = write_sweep_index(tmp_path, mx, cells, self._records(cells, video),
                                 image="face.png", voice="seed:felix_zh")
        html = path.read_text(encoding="utf-8")
        assert "网格对照" in html and "盲测" in html

    def test_embeds_only_playable_clips_in_blind_test(self, tmp_path):
        mx = resolve_matrix("screen10-models")
        cells = expand(mx)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        path = write_sweep_index(tmp_path, mx, cells, self._records(cells, video),
                                 image="face.png", voice="v")
        html = path.read_text(encoding="utf-8")
        payload = html.split("const CLIPS = ")[1].split(";\n")[0]
        clips = json.loads(payload)
        assert len(clips) == 1
        assert clips[0]["id"] == cells[0].id

    def test_pending_cells_render_without_video(self, tmp_path):
        mx = resolve_matrix("screen10-models")
        cells = expand(mx)
        path = write_sweep_index(tmp_path, mx, cells, [], image="f.png", voice="v")
        html = path.read_text(encoding="utf-8")
        assert html.count("class=\"card pending") == len(cells)

    def test_defects_shown(self, tmp_path):
        mx = resolve_matrix("screen10-models")
        cells = expand(mx)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        path = write_sweep_index(tmp_path, mx, cells, self._records(cells, video),
                                 image="f.png", voice="v")
        assert "lipsync_lag" in path.read_text(encoding="utf-8")
