#!/usr/bin/env python3
"""腾讯 TokenHub 客户端。不碰真接口：HTTP 全部替换掉。"""

from __future__ import annotations

import base64

import pytest

import tencentclient
from tencentclient import TokenHubError


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("TOKENHUB_API_KEY", "sk-test")
    monkeypatch.setattr(tencentclient.time, "sleep", lambda *_: None)


class TestConfig:
    def test_missing_key(self, monkeypatch):
        monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
        monkeypatch.delenv("TENCENT_TOKENHUB_API_KEY", raising=False)
        with pytest.raises(TokenHubError, match="TOKENHUB_API_KEY"):
            tencentclient.api_key()

    def test_alt_key_name(self, monkeypatch):
        monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
        monkeypatch.setenv("TENCENT_TOKENHUB_API_KEY", "sk-alt")
        assert tencentclient.api_key() == "sk-alt"

    def test_base_url_strips_slash(self, monkeypatch):
        monkeypatch.setenv("TOKENHUB_API_BASE", "https://example.com/")
        assert tencentclient.base_url() == "https://example.com"


class TestRecipes:
    def test_720_and_1080_share_model(self):
        a = tencentclient.recipe_spec("yt-video-humanactor")
        b = tencentclient.recipe_spec("yt-video-humanactor-1080")
        assert a["model"] == b["model"] == "yt-video-humanactor"
        assert a["resolution"] == "720p" and b["resolution"] == "1080p"

    def test_unknown_recipe(self):
        with pytest.raises(TokenHubError, match="未知腾讯方案"):
            tencentclient.recipe_spec("nope")

    def test_estimated_cny(self):
        assert tencentclient.estimated_cny("yt-video-humanactor", 10) == 12.0
        assert tencentclient.estimated_cny("yt-video-humanactor-1080", 10) == 24.0


class TestPayload:
    def test_local_image_becomes_base64(self, tmp_path, monkeypatch):
        img = tmp_path / "face.png"
        img.write_bytes(b"\x89PNG")
        monkeypatch.setattr(tencentclient, "public_url", lambda src: "https://cdn.example/a.mp3")
        payload = tencentclient.build_payload(
            "yt-video-humanactor", prompt="对着镜头讲话",
            audio="https://cdn.example/a.mp3", image=str(img))
        assert payload["model"] == "yt-video-humanactor"
        assert payload["audio_url"] == "https://cdn.example/a.mp3"
        assert payload["image_base64"] == base64.b64encode(b"\x89PNG").decode("ascii")
        assert "image_url" not in payload
        assert payload["resolution"] == "720p"
        assert payload["logo_add"] == 0

    def test_http_image_stays_url(self, monkeypatch):
        monkeypatch.setattr(tencentclient, "public_url", lambda src: src)
        payload = tencentclient.build_payload(
            "yt-video-humanactor-1080", prompt="p",
            audio="https://cdn.example/a.mp3",
            image="https://cdn.example/face.jpg")
        assert payload["image_url"] == "https://cdn.example/face.jpg"
        assert "image_base64" not in payload
        assert payload["resolution"] == "1080p"

    def test_public_url_passthrough(self):
        assert tencentclient.public_url("https://cos.example/a.mp3") == \
            "https://cos.example/a.mp3"


class TestSubmitQuery:
    def test_submit_reads_id(self, monkeypatch):
        monkeypatch.setattr(tencentclient, "_request",
                            lambda *a, **k: {"id": "job-1"})
        assert tencentclient.submit({"model": "yt-video-humanactor"}) == "job-1"

    def test_submit_reads_nested_job_id(self, monkeypatch):
        monkeypatch.setattr(tencentclient, "_request",
                            lambda *a, **k: {"Response": {"JobId": "138"}})
        assert tencentclient.submit({"model": "x"}) == "138"

    def test_submit_without_id_raises(self, monkeypatch):
        monkeypatch.setattr(tencentclient, "_request", lambda *a, **k: {"ok": True})
        with pytest.raises(TokenHubError, match="没返回任务号"):
            tencentclient.submit({"model": "x"})

    def test_query_done(self, monkeypatch):
        monkeypatch.setattr(tencentclient, "_request", lambda *a, **k: {
            "status": "DONE",
            "result_video_url": "https://cos.example/out.mp4",
        })
        out = tencentclient.query("yt-video-humanactor", "jid")
        assert out["url"] == "https://cos.example/out.mp4"
        assert out["status"] == "DONE"

    def test_query_reads_openai_nested_url(self, monkeypatch):
        """TokenHub 查询示例：status=completed，地址在 data.url。"""
        monkeypatch.setattr(tencentclient, "_request", lambda *a, **k: {
            "status": "completed",
            "data": {"url": "https://cos.example/out.mp4"},
        })
        out = tencentclient.query("yt-video-humanactor", "jid")
        assert out["status"] == "completed"
        assert out["url"] == "https://cos.example/out.mp4"

    def test_unwrap_http_string_data_as_url(self):
        flat = tencentclient._unwrap({
            "status": "completed",
            "data": "https://cos.example/out.mp4",
        })
        assert flat["url"] == "https://cos.example/out.mp4"

    def test_in_progress_is_wait(self):
        assert "in_progress" in tencentclient.STATUS_WAIT
        assert "completed" in tencentclient.STATUS_OK

    def test_poll_waits_then_done(self, monkeypatch):
        ticks = iter([
            {"status": "WAIT", "url": None, "error_code": "", "error_message": ""},
            {"status": "RUN", "url": None, "error_code": "", "error_message": ""},
            {"status": "DONE", "url": "https://cos.example/out.mp4",
             "error_code": "", "error_message": ""},
        ])
        monkeypatch.setattr(tencentclient, "query", lambda *a, **k: next(ticks))
        seen = []
        out = tencentclient.poll("yt-video-humanactor", "jid", interval=0,
                                 on_tick=lambda s, e: seen.append(s))
        assert out["url"].endswith("out.mp4")
        assert seen == ["WAIT", "RUN", "DONE"]

    def test_poll_fail_raises(self, monkeypatch):
        monkeypatch.setattr(tencentclient, "query", lambda *a, **k: {
            "status": "FAIL", "url": None, "error_code": "Moderation",
            "error_message": "审核未通过",
        })
        with pytest.raises(TokenHubError, match="审核未通过"):
            tencentclient.poll("yt-video-humanactor", "jid", interval=0)

    def test_http_401_mentions_key(self, monkeypatch):
        class Fake:
            status_code = 401
            text = "{}"

            def json(self):
                return {"message": "Unauthorized"}

        monkeypatch.setattr(tencentclient.requests, "request",
                            lambda *a, **k: Fake())
        with pytest.raises(TokenHubError, match="TOKENHUB_API_KEY"):
            tencentclient._request("POST", "/v1/api/video/submit", json_body={})
