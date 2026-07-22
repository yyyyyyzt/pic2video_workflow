"""Web 页面基础测试（无需真实推理 API）。"""

import io
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_app(tmp_jobs_dir: str):
    from web import app as app_module
    from web.job_store import JobStore

    store = JobStore(jobs_dir=tmp_jobs_dir)
    app_module.job_store = store
    return app_module.create_app(), store


def test_index_page():
    with tempfile.TemporaryDirectory() as d:
        app, _ = _make_app(os.path.join(d, "jobs"))
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "任务列表" in resp.get_data(as_text=True)
        print("index OK")


def test_health():
    with tempfile.TemporaryDirectory() as d:
        app, _ = _make_app(os.path.join(d, "jobs"))
        client = app.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        print("health OK")


def test_create_job_validation():
    with tempfile.TemporaryDirectory() as d:
        app, _ = _make_app(os.path.join(d, "jobs"))
        client = app.test_client()
        resp = client.post("/api/jobs")
        assert resp.status_code == 400
        print("validation OK")


def test_create_job_spawns_worker():
    with tempfile.TemporaryDirectory() as d:
        app, store = _make_app(os.path.join(d, "jobs"))
        client = app.test_client()
        fake_video = (io.BytesIO(b"fake"), "clip.mp4")
        fake_face = (io.BytesIO(b"fake"), "face.jpg")

        with patch("web.app._spawn_worker", return_value=12345) as spawn:
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
        spawn.assert_called_once()
        job = store.get(data["job_id"])
        assert job is not None
        assert store.get_manifest(data["job_id"]) is not None
        print("create job OK:", data["job_id"])


def test_list_jobs():
    with tempfile.TemporaryDirectory() as d:
        app, store = _make_app(os.path.join(d, "jobs"))
        store.create(video_name="a.mp4", face_name="b.jpg", duration=30, manifest={})
        client = app.test_client()
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert len(resp.get_json()["jobs"]) >= 1
        print("list jobs OK")


if __name__ == "__main__":
    test_index_page()
    test_health()
    test_create_job_validation()
    test_create_job_spawns_worker()
    test_list_jobs()
    print("\nALL WEB TESTS PASSED")
