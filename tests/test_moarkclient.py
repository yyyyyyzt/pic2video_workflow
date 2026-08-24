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
from moarkclient import MODELS, MoarkError


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


class TestModelTable:
    def test_every_model_declares_endpoint_and_required(self):
        for name, spec in MODELS.items():
            assert spec["endpoint"].startswith("/async/videos/"), name
            assert spec["required"], name
            assert isinstance(spec["available"], bool), name

    def test_file_fields_are_subset_of_required(self):
        for name, spec in MODELS.items():
            extra = set(spec["files"]) - set(spec["required"])
            assert not extra, f"{name} 的文件字段 {extra} 不在必填里"

    def test_infinitetalk_needs_cond_video(self):
        """实测：只给 image + cond_audio 会报「必传参数: cond_video」。"""
        assert "cond_video" in MODELS["InfiniteTalk"]["required"]

    def test_infinitetalk_marked_unavailable(self):
        # 实测平台侧 Service Temporarily Unavailable，别让人以为是自己参数写错
        assert MODELS["InfiniteTalk"]["available"] is False

    def test_base_url_is_gitee_not_moark(self):
        """真实 base 是 ai.gitee.com，api.moark.com 是错的。"""
        assert "ai.gitee.com" in moarkclient.BASE_URL


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
    def test_uses_multipart_when_files_given(self, monkeypatch, tmp_path):
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

    def test_uses_json_when_no_files(self, monkeypatch):
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw, url=url)
            return FakeResponse({"task_id": "T2"})

        monkeypatch.setattr(moarkclient.requests, "post", fake_post)
        moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p"})
        assert seen["json"]["prompt"] == "p"
        assert "files" not in seen

    def test_routes_to_model_endpoint(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(url=url),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("Duix-Avatar", files={"ref_image": str(f)})
        assert MODELS["Duix-Avatar"]["endpoint"] in seen["url"]

    def test_drops_empty_values(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(kw),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p", "empty": "", "none": None})
        assert "empty" not in seen["json"] and "none" not in seen["json"]

    def test_booleans_lowercased(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: (seen.update(kw),
                                               FakeResponse({"task_id": "T"}))[1])
        moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p", "flag": True})
        assert seen["json"]["flag"] == "true"

    def test_missing_task_id_raises(self, monkeypatch):
        monkeypatch.setattr(moarkclient.requests, "post",
                            lambda url, **kw: FakeResponse({"ok": 1}))
        with pytest.raises(MoarkError, match="task_id"):
            moarkclient.submit("Wan2.1-T2V-14B", {"prompt": "p"})

    def test_missing_field_error_explains_multipart(self, monkeypatch):
        monkeypatch.setattr(
            moarkclient.requests, "post",
            lambda url, **kw: FakeResponse(
                {"error": 400, "message": "必传参数: cond_video"}, status=400))
        with pytest.raises(MoarkError, match="multipart"):
            moarkclient.submit("InfiniteTalk", {"prompt": "p"})

    def test_post_is_not_retried(self, monkeypatch):
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
