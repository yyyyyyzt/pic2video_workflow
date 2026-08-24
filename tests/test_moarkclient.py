#!/usr/bin/env python3
"""模力方舟客户端。全部用假的 HTTP 层测，不碰真接口。

这些断言对应实测踩到的坑，改代码时不要放宽：
  - 状态词是 failure 不是 failed
  - 文件字段必须 multipart，不能塞 JSON
  - GET 要重试（一次读超时曾让整条付费任务白跑）
  - POST 不能重试（会重复提交、重复扣费）
"""

from __future__ import annotations

import pytest
import requests

import moarkclient
from moarkclient import MoarkError


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MOARK_API_KEY", "test-token")
    monkeypatch.setattr(moarkclient.time, "sleep", lambda *_: None)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


SAMPLE_MODELS = [
    {"id": "Duix-Avatar", "description": "数字人",
     "operations": [{"type": "audio_video2video", "name": "数字人生成",
                     "path": "v1/async/videos/audio-video-to-video",
                     "price": "0.0100", "unit_tag": {"name": "秒"}}]},
    {"id": "LTX-2", "description": "音视频",
     "operations": [{"type": "image2video", "name": "图生视频",
                     "path": "v1/async/videos/image-to-video",
                     "price": "0.3000", "unit_tag": {"name": "秒"}}]},
    {"id": "Wan2.7", "description": "多能力",
     "operations": [
         {"type": "text2video", "name": "文生视频",
          "path": "v1/async/videos/generations", "price": "0.1"},
         {"type": "image_video2video", "name": "视频编辑",
          "path": "v1/async/videos/image-video-to-video", "price": "0.1"}]},
    {"id": "HunyuanOCR", "description": "OCR",
     "operations": [{"type": "image2text", "name": "OCR",
                     "path": "v1/images/ocr", "price": "0.02"}]},
    {"id": "no-ops", "description": "没有 operations", "operations": []},
]


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    """用固定的清单替掉网络请求。"""
    monkeypatch.setattr(moarkclient, "MODEL_CACHE", tmp_path / "models.json")
    monkeypatch.setattr(moarkclient, "model_details",
                        lambda **kw: SAMPLE_MODELS)
    return SAMPLE_MODELS


class TestBaseUrl:
    def test_defaults_to_moark(self):
        """api.moark.com 和 ai.gitee.com 是同一后端，实测两边余额完全一致。"""
        assert "api.moark.com" in moarkclient.BASE_URL

    def test_override(self, monkeypatch):
        monkeypatch.setenv("MOARK_API_BASE", "https://ai.gitee.com/v1/")
        assert moarkclient.base_url() == "https://ai.gitee.com/v1"


class TestDiscovery:
    def test_normalises_v1_prefix(self):
        assert moarkclient._normalise_path("v1/async/videos/x") == "/async/videos/x"
        assert moarkclient._normalise_path("/async/videos/x") == "/async/videos/x"

    def test_keeps_only_supported_endpoints(self, catalog):
        found = moarkclient.video_models()
        assert "HunyuanOCR" not in found       # OCR 端点不支持
        assert "no-ops" not in found
        assert "Duix-Avatar" in found

    def test_reads_endpoint_price_and_unit(self, catalog):
        entry = moarkclient.video_models()["Duix-Avatar"]
        assert entry["endpoint"] == moarkclient.EP_AUDIO_VIDEO_TO_VIDEO
        assert entry["price"] == pytest.approx(0.01)
        assert entry["unit"] == "秒"
        assert entry["kind"] == "audio_video2video"

    def test_prefers_avatar_operation_when_model_has_several(self, catalog):
        """Wan2.7 同时挂文生视频和视频编辑，应该取更靠近数字人的那个。"""
        assert moarkclient.video_models()["Wan2.7"]["kind"] == "image_video2video"

    def test_missing_unit_falls_back(self, catalog):
        assert moarkclient.video_models()["Wan2.7"]["unit"] == "次"

    def test_avatar_models_filters_by_kind(self, catalog):
        avatars = moarkclient.avatar_models()
        assert "Duix-Avatar" in avatars and "LTX-2" in avatars
        assert "Wan2.7" not in avatars          # 视频编辑不算数字人

    def test_endpoint_for_uses_catalog(self, catalog):
        assert moarkclient.endpoint_for("Duix-Avatar") == \
            moarkclient.EP_AUDIO_VIDEO_TO_VIDEO

    def test_endpoint_for_unknown_falls_back(self, catalog):
        assert moarkclient.endpoint_for("没这个模型") == moarkclient.EP_IMAGE_TO_VIDEO

    def test_endpoint_for_survives_network_failure(self, monkeypatch):
        def boom(**kw):
            raise moarkclient.MoarkError("网络挂了")

        monkeypatch.setattr(moarkclient, "model_details", boom)
        assert moarkclient.endpoint_for("Duix-Avatar") == moarkclient.EP_IMAGE_TO_VIDEO

    def test_cache_is_used(self, monkeypatch, tmp_path):
        cache = tmp_path / "m.json"
        cache.write_text(json_dumps(SAMPLE_MODELS), encoding="utf-8")
        monkeypatch.setattr(moarkclient, "_get_with_retry",
                            lambda *a, **k: pytest.fail("不该联网"))
        assert moarkclient.model_details(cache=cache)

    def test_corrupt_cache_refetches(self, monkeypatch, tmp_path):
        cache = tmp_path / "m.json"
        cache.write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(moarkclient, "_get_with_retry",
                            lambda *a, **k: {"data": SAMPLE_MODELS})
        assert len(moarkclient.model_details(cache=cache)) == len(SAMPLE_MODELS)


class TestResolveFiles:
    def test_avatar_endpoint_maps_audio_and_video(self):
        """实测数字人端点要的是 ref_audio + ref_video。"""
        files, missing = moarkclient.resolve_files(
            moarkclient.EP_AUDIO_VIDEO_TO_VIDEO, audio="a.mp3", video="v.mp4")
        assert files == {"ref_audio": "a.mp3", "ref_video": "v.mp4"}
        assert missing == []

    def test_reports_missing_template_video(self):
        _, missing = moarkclient.resolve_files(
            moarkclient.EP_AUDIO_VIDEO_TO_VIDEO, audio="a.mp3")
        assert missing == ["ref_video"]

    def test_image_endpoint_maps_cond_fields(self):
        files, _ = moarkclient.resolve_files(
            moarkclient.EP_IMAGE_TO_VIDEO, image="f.png", audio="a.mp3", video="v.mp4")
        assert files == {"image": "f.png", "cond_audio": "a.mp3",
                         "cond_video": "v.mp4"}

    def test_image_endpoint_requires_image(self):
        _, missing = moarkclient.resolve_files(
            moarkclient.EP_IMAGE_TO_VIDEO, audio="a.mp3")
        assert missing == ["image"]

    def test_text_endpoint_needs_nothing(self):
        files, missing = moarkclient.resolve_files(moarkclient.EP_TEXT_TO_VIDEO)
        assert files == {} and missing == []

    def test_unknown_role_is_dropped(self):
        files, _ = moarkclient.resolve_files(
            moarkclient.EP_AUDIO_VIDEO_TO_VIDEO, image="f.png",
            audio="a.mp3", video="v.mp4")
        assert "f.png" not in files.values()     # 该端点不吃图


def json_dumps(obj):
    import json

    return json.dumps(obj, ensure_ascii=False)


class TestErrorDetail:
    def test_dict_shaped_error(self):
        data = {"output": {"error": {"code": 500, "message": "Service Unavailable"}}}
        assert moarkclient._error_detail(data) == "Service Unavailable"

    def test_string_shaped_error(self):
        """实测两种形状都出现过，直接 .get 会 AttributeError。"""
        data = {"output": {"error": "An unexpected error has occurred."}}
        assert "unexpected" in moarkclient._error_detail(data)

    def test_no_output_falls_back_to_status(self):
        assert moarkclient._error_detail({"status": "failure"}) == "failure"

    def test_output_is_none(self):
        assert moarkclient._error_detail({"output": None, "status": "failure"})


class TestStatusVocabulary:
    def test_failure_is_terminal(self):
        assert "failure" in moarkclient.STATUS_BAD

    def test_success_is_terminal(self):
        assert "success" in moarkclient.STATUS_OK

    def test_in_progress_is_not_terminal(self):
        assert "in_progress" not in moarkclient.STATUS_OK
        assert "in_progress" not in moarkclient.STATUS_BAD

    def test_waiting_is_not_terminal(self):
        assert "waiting" not in moarkclient.STATUS_OK | moarkclient.STATUS_BAD


class TestSubmit:
    def test_uses_multipart_when_files_given(self, monkeypatch, tmp_path, catalog):
        f = tmp_path / "a.png"
        f.write_bytes(b"x" * 32)
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw, url=url)
            return FakeResponse({"task_id": "T1"})

        monkeypatch.setattr(moarkclient.requests, "post", fake_post)
        assert moarkclient.submit("Wan2_2-I2V-A14B", {"prompt": "p"},
                                  files={"image": str(f)}) == "T1"
        assert "files" in seen and "image" in seen["files"]
        assert seen["data"]["model"] == "Wan2_2-I2V-A14B"
        # multipart 时不能自己设 Content-Type，否则 boundary 会丢
        assert "Content-Type" not in seen["headers"]

    def test_uses_json_when_no_files(self, monkeypatch, catalog):
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw, url=url)
            return FakeResponse({"task_id": "T2"})

        monkeypatch.setattr(moarkclient.requests, "post", fake_post)
        moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p"})
        assert seen["json"]["prompt"] == "p"
        assert "files" not in seen

    def test_routes_to_endpoint_from_catalog(self, monkeypatch, tmp_path, catalog):
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(url=url),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("Duix-Avatar", files={"ref_audio": str(f)})
        assert moarkclient.EP_AUDIO_VIDEO_TO_VIDEO in seen["url"]

    def test_explicit_endpoint_wins(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(url=url),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("X", files={"image": str(f)},
                           endpoint=moarkclient.EP_MULTIMODAL)
        assert moarkclient.EP_MULTIMODAL in seen["url"]

    def test_drops_empty_values(self, monkeypatch, catalog):
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(kw),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p", "empty": "", "none": None})
        assert "empty" not in seen["json"] and "none" not in seen["json"]

    def test_booleans_lowercased(self, monkeypatch, catalog):
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(kw),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p", "flag": True})
        assert seen["json"]["flag"] == "true"

    def test_missing_task_id_raises(self, monkeypatch, catalog):
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: FakeResponse({"ok": 1}))
        with pytest.raises(MoarkError, match="task_id"):
            moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p"})

    def test_missing_field_error_explains_multipart(self, monkeypatch, catalog):
        monkeypatch.setattr(
            moarkclient.requests, "post",
            lambda url, **kw: FakeResponse(
                {"error": 400, "message": "必传参数: cond_video"}, status=400))
        with pytest.raises(MoarkError, match="multipart"):
            moarkclient.submit("InfiniteTalk", {"prompt": "p"})

    def test_post_is_not_retried(self, monkeypatch, catalog):
        """提交不能重试：读超时后无法判断服务端是否已建任务，重试会重复扣费。"""
        calls = []

        def fake_post(url, **kw):
            calls.append(1)
            raise requests.Timeout("boom")

        monkeypatch.setattr(moarkclient.requests, "post", fake_post)
        with pytest.raises(requests.Timeout):
            moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p"})
        assert len(calls) == 1


class TestPoll:
    def _responses(self, monkeypatch, sequence):
        state = {"i": 0}

        def fake_get(url, **kw):
            item = sequence[min(state["i"], len(sequence) - 1)]
            state["i"] += 1
            if isinstance(item, Exception):
                raise item
            return FakeResponse(item)

        monkeypatch.setattr(moarkclient.requests, "get", fake_get)
        return state

    def test_returns_url_and_price_on_success(self, monkeypatch):
        self._responses(monkeypatch, [
            {"status": "waiting"},
            {"status": "success", "output": {"file_url": "http://x/v.mp4"},
             "price": 1.5, "currency": "CNY"},
        ])
        out = moarkclient.poll("T", interval=0)
        assert out["url"] == "http://x/v.mp4"
        assert out["price"] == 1.5 and out["currency"] == "CNY"

    def test_failure_status_stops_immediately(self, monkeypatch):
        """核心回归：failure 必须是终态，否则会一直轮询到超时。"""
        self._responses(monkeypatch, [
            {"status": "failure",
             "output": {"error": {"code": 500, "message": "Service Temporarily Unavailable"}}},
        ])
        with pytest.raises(MoarkError, match="Service Temporarily Unavailable"):
            moarkclient.poll("T", interval=0)

    def test_in_progress_then_success(self, monkeypatch):
        self._responses(monkeypatch, [
            {"status": "waiting"}, {"status": "in_progress"},
            {"status": "success", "output": {"file_url": "u"}},
        ])
        assert moarkclient.poll("T", interval=0)["url"] == "u"

    def test_success_without_url_raises(self, monkeypatch):
        self._responses(monkeypatch, [{"status": "success", "output": {}}])
        with pytest.raises(MoarkError, match="file_url"):
            moarkclient.poll("T", interval=0)

    def test_transient_timeout_is_retried(self, monkeypatch):
        """核心回归：轮询时一次读超时不该把整条付费任务判死。"""
        self._responses(monkeypatch, [
            requests.Timeout("read timeout"),
            {"status": "success", "output": {"file_url": "u"}},
        ])
        assert moarkclient.poll("T", interval=0)["url"] == "u"

    def test_gives_up_after_retry_budget(self, monkeypatch):
        self._responses(monkeypatch, [requests.ConnectionError("down")])
        with pytest.raises(MoarkError, match="网络失败"):
            moarkclient.poll("T", interval=0)

    def test_on_tick_receives_status(self, monkeypatch):
        self._responses(monkeypatch, [
            {"status": "in_progress"},
            {"status": "success", "output": {"file_url": "u"}},
        ])
        seen = []
        moarkclient.poll("T", interval=0, on_tick=lambda s, e: seen.append(s))
        assert "in_progress" in seen


class TestRun:
    def test_on_submit_gets_task_id_before_polling(self, monkeypatch):
        """拿到任务号必须立刻回调：本地挂了还能凭它把已付费的结果取回来。"""
        monkeypatch.setattr(moarkclient, "submit", lambda *a, **k: "TID")
        order = []
        monkeypatch.setattr(moarkclient, "poll",
                            lambda *a, **k: order.append("poll") or
                            {"url": "u", "price": None, "currency": "CNY"})
        moarkclient.run("Wan2.1-T2V-14B", {"prompt": "p"},
                        on_submit=lambda t: order.append(f"submit:{t}"))
        assert order == ["submit:TID", "poll"]


class TestApiKey:
    def test_missing_key_message_is_actionable(self, monkeypatch):
        monkeypatch.delenv("MOARK_API_KEY", raising=False)
        with pytest.raises(MoarkError, match="MOARK_API_KEY"):
            moarkclient.api_key()

    def test_whitespace_only_counts_as_missing(self, monkeypatch):
        monkeypatch.setenv("MOARK_API_KEY", "   ")
        with pytest.raises(MoarkError):
            moarkclient.api_key()

    def test_base_url_override(self, monkeypatch):
        monkeypatch.setenv("MOARK_API_BASE", "https://example.com/v9/")
        assert moarkclient.base_url() == "https://example.com/v9"
