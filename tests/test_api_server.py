"""FastAPI 服务集成测试（fake 引擎）。"""

import json
import os
import tempfile
import time

# 必须在导入 server.app 之前设置环境（模块级读取配置）
_TMP = tempfile.mkdtemp(prefix="scailswap_api_test_")
os.environ["SCAILSWAP_ENGINE"] = "fake"
os.environ["SCAILSWAP_DATA_DIR"] = _TMP

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.app import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def sample_files():
    video_path = os.path.join(_TMP, "drv.mp4")
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (64, 64))
    for i in range(20):
        frame = np.zeros((64, 64, 3), np.uint8)
        frame[..., 2] = min(255, i * 12)
        writer.write(frame)
    writer.release()

    image_path = os.path.join(_TMP, "face.png")
    cv2.imwrite(image_path, np.full((64, 64, 3), 128, np.uint8))
    return image_path, video_path


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"]["engine"] == "fake"
    assert body["ok"] is True


def test_job_lifecycle(client, sample_files):
    image_path, video_path = sample_files
    with open(image_path, "rb") as img, open(video_path, "rb") as vid:
        resp = client.post(
            "/api/v1/jobs",
            files={
                "source_image": ("face.png", img, "image/png"),
                "target_video": ("drv.mp4", vid, "video/mp4"),
            },
            data={
                "prompt": "测试",
                "mode": "replacement",
                "seed": "7",
                "params_json": json.dumps({"window_frames": 13, "overlap_frames": 5}),
            },
        )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    # 轮询直至完成（fake 引擎应在数秒内完成）
    deadline = time.time() + 60
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.3)
    assert status["status"] == "done", status
    assert status["percent"] == 100.0
    assert status["download_url"]

    # 下载结果
    dl = client.get(f"/api/v1/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "video/mp4"
    assert len(dl.content) > 1000

    # 任务列表包含该任务
    listing = client.get("/api/v1/jobs").json()
    assert any(j["job_id"] == job_id for j in listing)


def test_invalid_params_rejected(client, sample_files):
    image_path, video_path = sample_files
    with open(image_path, "rb") as img, open(video_path, "rb") as vid:
        resp = client.post(
            "/api/v1/jobs",
            files={
                "source_image": ("face.png", img, "image/png"),
                "target_video": ("drv.mp4", vid, "video/mp4"),
            },
            data={"params_json": json.dumps({"not_a_field": 1})},
        )
    assert resp.status_code == 400
    assert "未知字段" in resp.text


def test_missing_job_404(client):
    assert client.get("/api/v1/jobs/nonexistent").status_code == 404
