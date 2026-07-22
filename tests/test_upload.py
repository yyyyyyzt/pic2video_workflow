"""上传与 base64 输入解析测试。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roleswap.client import RoleSwapClient
from roleswap.config import RoleSwapConfig
from roleswap.upload_utils import encode_as_data_uri, parse_upload_response


def test_encode_as_data_uri():
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(b"fake-image")
        path = tmp.name
    try:
        uri = encode_as_data_uri(path, kind="image")
        assert uri.startswith("data:image/jpeg;base64,")
        print("encode_as_data_uri OK")
    finally:
        os.unlink(path)


def test_parse_upload_response():
    assert parse_upload_response({"name": "a.png", "subfolder": "input"}) == "input/a.png"
    assert parse_upload_response({"filename": "b.mp4"}) == "b.mp4"
    assert parse_upload_response({"url": "https://x/f.mp4"}) == "https://x/f.mp4"
    print("parse_upload_response OK")


def test_resolve_input_uses_base64_by_default():
    config = RoleSwapConfig(
        base_url="https://host.test",
        workflow_id="wf",
        input_mode="base64",
    )
    client = RoleSwapClient(config=config)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(b"face")
        path = tmp.name
    try:
        resolved = client._resolve_input(path, kind="image")
        assert resolved.startswith("data:image/jpeg;base64,")
        # 缓存命中
        assert client._resolve_input(path, kind="image") == resolved
        print("resolve base64 OK")
    finally:
        os.unlink(path)


def test_auto_mode_falls_back_to_base64_on_405():
    from tests.test_client import FakeResponse, FakeSession

    config = RoleSwapConfig(
        base_url="https://host.test",
        workflow_id="wf",
        input_mode="auto",
    )
    session = FakeSession(
        post_responses=[FakeResponse(405, {"error": "Method Not Allowed"})] * 20,
        get_responses=[],
    )
    client = RoleSwapClient(config=config, session=session)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(b"face")
        path = tmp.name
    try:
        resolved = client._resolve_input(path, kind="image")
        assert resolved.startswith("data:")
        print("auto fallback OK")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_encode_as_data_uri()
    test_parse_upload_response()
    test_resolve_input_uses_base64_by_default()
    test_auto_mode_falls_back_to_base64_on_405()
    print("\nALL UPLOAD TESTS PASSED")
