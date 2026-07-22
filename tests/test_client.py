"""RoleSwapClient 网络流程测试：用假 Session 验证提交 / 轮询 / 解析逻辑。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roleswap.client import RoleSwapClient, RoleSwapError
from roleswap.config import RoleSwapConfig


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._content = content
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    # 供 download 的 stream 使用
    def iter_content(self, chunk_size=1):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, post_responses, get_responses):
        self._post = list(post_responses)
        self._get = list(get_responses)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get.pop(0)


def _config():
    return RoleSwapConfig(
        base_url="https://host.test",
        workflow_id="wf-1",
        poll_interval=0.0,
        result_timeout=5,
    )


def test_submit_builds_payload_and_returns_prompt_id():
    session = FakeSession(
        post_responses=[FakeResponse(200, {"prompt_id": "abc123"})],
        get_responses=[],
    )
    client = RoleSwapClient(config=_config(), session=session)
    pid = client.submit(
        video="https://x/v.mp4", face_image="https://x/f.jpg", seed=7
    )
    assert pid == "abc123"
    url, kwargs = session.post_calls[0]
    assert url.endswith("/api/workflow/generate")
    body = kwargs["json"]
    assert body["workflow_id"] == "wf-1"
    values = body["input_values"]
    assert values["42:steps"] == 6
    assert values["42:scheduler"] == "dpm++_sde"
    assert values["51:blocks_to_swap"] == 40
    assert values["151:value"] is False
    print("submit OK")


def test_infer_poll_status_pending():
    client = RoleSwapClient(config=_config())
    assert client._infer_poll_status(
        {"success": True, "pending": True, "results": []}
    ) == "pending"
    assert client._infer_poll_status({"status": "processing"}) == "running"
    assert client._infer_poll_status({"running": True}) == "running"
    print("infer_poll_status OK")


def test_format_poll_detail():
    client = RoleSwapClient(config=_config())
    detail = client._format_poll_detail(
        data={"progress": 0.35, "queue_position": 2},
        status="running",
        poll_count=12,
        queue_hint="排队第 1 位",
    )
    assert "GPU 推理中" in detail
    assert "35%" in detail
    assert "轮询 #12" in detail
    print("format_poll_detail OK")


def test_parse_queue_for_prompt():
    data = {
        "queue_running": [["abc", {"prompt_id": "p1"}]],
        "queue_pending": [["def"], ["ghi", "p2"]],
    }
    assert RoleSwapClient._parse_queue_for_prompt(data, "p2") is not None
    print("parse_queue OK")


def test_infer_poll_status_pending_false_with_results():
    client = RoleSwapClient(config=_config())
    data = {
        "success": True,
        "pending": False,
        "results": [
            {
                "type": "image",
                "url": "/api/comfy/view?filename=ComfyUI_temp_x.png&type=temp",
                "raw": {"filename": "ComfyUI_temp_x.png", "type": "temp"},
            }
        ],
    }
    assert client._infer_poll_status(data) == "completed"
    assert client._extract_output_url(data) is None
    err = client._format_missing_video_error("pid-1", data)
    assert "未生成视频" in err
    assert "ComfyUI_temp" in err
    print("pending_false temp result OK")


def test_pick_video_from_results():
    client = RoleSwapClient(config=_config())
    results = [
        {
            "type": "image",
            "url": "/api/comfy/view?filename=ComfyUI_temp_a.png&type=temp",
            "raw": {"filename": "ComfyUI_temp_a.png", "type": "temp"},
        },
        {
            "type": "video",
            "url": "/api/comfy/view?filename=AnimateDiff_00003.mp4&type=output&subfolder=Scail2",
            "raw": {"filename": "AnimateDiff_00003.mp4", "subfolder": "Scail2", "type": "output"},
        },
    ]
    url = client._pick_video_from_results(results)
    assert url and "AnimateDiff_00003.mp4" in url
    print("pick_video_from_results OK")


def test_wait_for_result_fails_fast_on_temp_only():
    temp_only = {
        "success": True,
        "pending": False,
        "prompt_id": "p-temp",
        "results": [
            {
                "type": "image",
                "url": "/api/comfy/view?filename=ComfyUI_temp_b.png&type=temp",
                "raw": {"filename": "ComfyUI_temp_b.png", "type": "temp"},
            }
        ],
    }
    session = FakeSession(
        post_responses=[],
        get_responses=[FakeResponse(200, temp_only)],
    )
    client = RoleSwapClient(config=_config(), session=session)
    try:
        client.wait_for_result("p-temp")
        raise AssertionError("should raise")
    except RoleSwapError as exc:
        assert "未生成视频" in str(exc)
        assert len(session.get_calls) == 1
    print("fail fast temp only OK")


def test_wait_for_result_polls_until_done():
    session = FakeSession(
        post_responses=[],
        get_responses=[
            FakeResponse(200, {"status": "processing"}),
            FakeResponse(200, {"status": "completed",
                               "outputs": [{"url": "https://x/out.mp4"}]}),
        ],
    )
    client = RoleSwapClient(config=_config(), session=session)
    url = client.wait_for_result("abc123")
    assert url == "https://x/out.mp4"
    assert len(session.get_calls) == 2
    print("wait_for_result OK")


def test_wait_for_result_pending_extends_wait():
    pending = {"success": True, "pending": True, "prompt_id": "p1", "results": []}
    done = {
        "success": True,
        "pending": False,
        "outputs": [{"url": "https://x/out.mp4"}],
    }
    session = FakeSession(
        post_responses=[],
        get_responses=[FakeResponse(200, pending)] * 5 + [FakeResponse(200, done)],
    )
    cfg = _config()
    cfg.result_timeout = 1
    cfg.poll_interval = 0.0
    client = RoleSwapClient(config=cfg, session=session)
    url = client.wait_for_result("p1")
    assert url == "https://x/out.mp4"
    assert len(session.get_calls) >= 6
    print("wait_for_result pending extend OK")


def test_extract_output_url_variants():
    client = RoleSwapClient(config=_config())
    assert client._extract_output_url(
        {"video_url": "https://x/a.mp4"}) == "https://x/a.mp4"
    assert client._extract_output_url(
        {"result": {"filename": "b.mp4"}}) is not None
    url = client._extract_output_url({
        "outputs": {
            "62": {
                "gifs": [{
                    "filename": "Scail2_AnimateDiff_00001.mp4",
                    "subfolder": "",
                    "type": "output",
                }]
            }
        }
    })
    assert url and "Scail2_AnimateDiff_00001.mp4" in url
    print("extract_output_url OK")


def test_fix_view_url_strips_output_prefix():
    client = RoleSwapClient(config=_config())
    bad = (
        "https://host.test/api/comfy/view?"
        "filename=%2Foutput%2FScail2%2FAnimateDiff_00003.mp4&type=output"
    )
    fixed = client._fix_view_url(bad)
    assert "filename=AnimateDiff_00003.mp4" in fixed
    assert "subfolder=Scail2" in fixed
    assert "%2Foutput%2F" not in fixed
    print("fix_view_url OK")


def test_normalize_output_ref_with_path():
    client = RoleSwapClient(config=_config())
    url = client._normalize_output_ref("/output/Scail2/AnimateDiff_00003.mp4")
    assert "filename=AnimateDiff_00003.mp4" in url
    assert "subfolder=Scail2" in url
    print("normalize_output_ref path OK")


def test_download_writes_file(tmp_path=None):
    import tempfile
    session = FakeSession(
        post_responses=[],
        get_responses=[
            FakeResponse(502, {"error": "File not found"}),
            FakeResponse(200, {}, content=b"BINARYDATA"),
        ],
    )
    client = RoleSwapClient(config=_config(), session=session)
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "out.mp4")
        bad = (
            "https://host.test/api/comfy/view?"
            "filename=%2Foutput%2FScail2%2Fout.mp4&type=output"
        )
        client.download(bad, dest)
        with open(dest, "rb") as fh:
            assert fh.read() == b"BINARYDATA"
    print("download OK")


if __name__ == "__main__":
    test_submit_builds_payload_and_returns_prompt_id()
    test_infer_poll_status_pending()
    test_infer_poll_status_pending_false_with_results()
    test_pick_video_from_results()
    test_wait_for_result_fails_fast_on_temp_only()
    test_format_poll_detail()
    test_parse_queue_for_prompt()
    test_wait_for_result_polls_until_done()
    test_wait_for_result_pending_extends_wait()
    test_extract_output_url_variants()
    test_fix_view_url_strips_output_prefix()
    test_normalize_output_ref_with_path()
    test_download_writes_file()
    print("\nALL CLIENT TESTS PASSED")
