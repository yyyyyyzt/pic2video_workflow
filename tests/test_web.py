"""Web 页面基础测试（无需真实推理 API）。"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import create_app


def test_index_page():
    app = create_app()
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "RoleSwap" in resp.get_data(as_text=True)
    print("index OK")


def test_health():
    app = create_app()
    client = app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    print("health OK")


def test_create_job_validation():
    app = create_app()
    client = app.test_client()
    resp = client.post("/api/jobs")
    assert resp.status_code == 400
    print("validation OK")


def test_create_job_accepts_upload(monkeypatch=None):
    """验证表单上传能被接受并创建任务（mock 后台生成）。"""
    import threading
    from unittest.mock import patch

    app = create_app()
    client = app.test_client()

    fake_video = (io.BytesIO(b"fake"), "clip.mp4")
    fake_face = (io.BytesIO(b"fake"), "face.jpg")

    with patch("web.app.generate_digital_human", return_value="/tmp/fake.mp4"):
        with patch("web.app._run_job", side_effect=lambda **kw: None):
            # 直接 patch thread 避免真正启动后台
            orig_thread = threading.Thread

            class ImmediateThread(orig_thread):
                def start(self):
                    return None

            with patch("web.app.threading.Thread", ImmediateThread):
                resp = client.post(
                    "/api/jobs",
                    data={
                        "video": fake_video,
                        "face": fake_face,
                        "duration": "10",
                        "steps": "6",
                        "cfg": "1.0",
                        "shift": "5.0",
                        "max_parallel": "1",
                    },
                    content_type="multipart/form-data",
                )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "job_id" in data
    print("upload OK:", data["job_id"])


if __name__ == "__main__":
    test_index_page()
    test_health()
    test_create_job_validation()
    test_create_job_accepts_upload()
    print("\nALL WEB TESTS PASSED")
