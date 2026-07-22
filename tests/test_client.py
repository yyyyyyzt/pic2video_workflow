"""RoleSwapClient 网络流程测试：用假 Session 验证提交 / 轮询 / 解析逻辑。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roleswap.client import RoleSwapClient
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


def test_download_writes_file(tmp_path=None):
    import tempfile
    session = FakeSession(
        post_responses=[],
        get_responses=[FakeResponse(200, {}, content=b"BINARYDATA")],
    )
    client = RoleSwapClient(config=_config(), session=session)
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "out.mp4")
        client.download("https://x/out.mp4", dest)
        with open(dest, "rb") as fh:
            assert fh.read() == b"BINARYDATA"
    print("download OK")


if __name__ == "__main__":
    test_submit_builds_payload_and_returns_prompt_id()
    test_wait_for_result_polls_until_done()
    test_extract_output_url_variants()
    test_download_writes_file()
    print("\nALL CLIENT TESTS PASSED")
