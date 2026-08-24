#!/usr/bin/env python3
"""调试台。不碰真接口：生成那一步整个替换掉。

这些测试守的是「运营用得下去」这条线：校验要给人话、任务列表要活过重启、
付过钱的任务不能因为本地一次超时就丢。
"""

from __future__ import annotations

import json

import pytest

import studio
from studio import Job, JobStore

fastapi = pytest.importorskip("fastapi", reason="没装 fastapi")
from fastapi.testclient import TestClient           # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个测试独立的数据目录，避免互相看到对方的任务。"""
    monkeypatch.setattr(studio, "DATA_DIR", tmp_path)
    monkeypatch.setattr(studio, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(studio, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(studio, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(studio, "STORE", JobStore(tmp_path / "jobs.json"))
    # 默认不真跑生成，各测试自己决定要不要接管
    monkeypatch.setattr(studio, "_run_job", lambda job_id: None)
    return TestClient(studio.create_app())


def _png() -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 40)


def _submit(client, **overrides):
    data = {"provider": "wavespeed", "recipe": "skyreels-std",
            "prompt": "p", "seconds": "10", "script": "一段台词"}
    data.update(overrides)
    files = {"image": ("f.png", _png(), "image/png")}
    return client.post("/api/jobs", data=data, files=files)


class TestJobStore:
    def test_survives_restart(self, tmp_path):
        path = tmp_path / "jobs.json"
        store = JobStore(path)
        store.add(Job(id="a", created_at="2026-01-01T00:00:00",
                      provider="wavespeed", recipe="r", prompt="p", seconds=10))
        assert JobStore(path).get("a") is not None

    def test_corrupt_file_does_not_crash(self, tmp_path):
        path = tmp_path / "jobs.json"
        path.write_text("{not json", encoding="utf-8")
        assert JobStore(path).list() == []

    def test_unknown_fields_are_skipped(self, tmp_path):
        path = tmp_path / "jobs.json"
        path.write_text(json.dumps([{"id": "x", "surprise": 1}]), encoding="utf-8")
        assert JobStore(path).list() == []

    def test_newest_first(self, tmp_path):
        store = JobStore(tmp_path / "jobs.json")
        for i, ts in enumerate(["2026-01-01T00:00:00", "2026-06-01T00:00:00"]):
            store.add(Job(id=str(i), created_at=ts, provider="w",
                          recipe="r", prompt="", seconds=1))
        assert [j.id for j in store.list()] == ["1", "0"]

    def test_update_persists(self, tmp_path):
        path = tmp_path / "jobs.json"
        store = JobStore(path)
        store.add(Job(id="a", created_at="t", provider="w", recipe="r",
                      prompt="", seconds=1))
        store.update("a", status="done", cost=1.25)
        reloaded = JobStore(path).get("a")
        assert reloaded.status == "done" and reloaded.cost == 1.25

    def test_update_unknown_id_is_noop(self, tmp_path):
        JobStore(tmp_path / "jobs.json").update("nope", status="done")


class TestConfigEndpoint:
    def test_exposes_everything_the_page_needs(self, client):
        data = client.get("/api/config").json()
        assert set(data) == {"models", "prompt", "voices",
                             "default_voice", "chars_per_second"}
        assert data["models"] and data["voices"]

    def test_model_entries_have_provider_and_recipe(self, client):
        for m in client.get("/api/config").json()["models"]:
            assert m["provider"] in ("wavespeed", "moark")
            assert m["recipe"] and m["label"]

    def test_studio_models_reference_real_recipes(self, client):
        import moarkclient
        from recipes import RECIPES

        for m in client.get("/api/config").json()["models"]:
            table = RECIPES if m["provider"] == "wavespeed" else moarkclient.MODELS
            assert m["recipe"] in table, f"{m['recipe']} 不存在"

    def test_labels_mention_price(self, client):
        """运营选模型之前就该知道贵不贵——OmniHuman 一条 80 秒就是 $12.6。"""
        for m in client.get("/api/config").json()["models"]:
            if m["provider"] == "wavespeed":
                assert "$" in m["label"] or "未实测" in m["label"]


class TestValidation:
    def test_missing_image_returns_400_with_chinese(self, client):
        r = client.post("/api/jobs", data={"recipe": "skyreels-std"})
        assert r.status_code == 400
        assert "角色图" in r.json()["error"]

    def test_missing_audio_and_script_returns_400(self, client):
        r = client.post("/api/jobs", data={"recipe": "skyreels-std", "prompt": "p"},
                        files={"image": ("f.png", _png(), "image/png")})
        assert r.status_code == 400
        assert "台词" in r.json()["error"]

    def test_script_only_is_accepted(self, client):
        assert _submit(client).status_code == 200

    def test_audio_only_is_accepted(self, client):
        r = client.post("/api/jobs",
                        data={"recipe": "skyreels-std", "prompt": "p", "script": ""},
                        files={"image": ("f.png", _png(), "image/png"),
                               "audio": ("a.mp3", b"ID3fake", "audio/mpeg")})
        assert r.status_code == 200


class TestJobLifecycle:
    def test_created_job_is_listed(self, client):
        job_id = _submit(client).json()["id"]
        assert any(j["id"] == job_id for j in client.get("/api/jobs").json())

    def test_records_prompt_and_blocks(self, client):
        blocks = {"gesture": "counting", "expression": "earnest"}
        job_id = _submit(client, prompt="手指点数",
                         blocks=json.dumps(blocks), preset="counting").json()["id"]
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["prompt"] == "手指点数"
        assert job["blocks"] == blocks
        assert job["preset"] == "counting"

    def test_malformed_blocks_json_does_not_break(self, client):
        job_id = _submit(client, blocks="{oops").json()["id"]
        assert client.get(f"/api/jobs/{job_id}").json()["blocks"] == {}

    def test_uploads_are_saved(self, client, tmp_path):
        from pathlib import Path

        job_id = _submit(client).json()["id"]
        image = client.get(f"/api/jobs/{job_id}").json()["image"]
        assert Path(image).is_file()

    def test_unknown_job_404(self, client):
        assert client.get("/api/jobs/nope").status_code == 404

    def test_media_not_ready_404(self, client):
        job_id = _submit(client).json()["id"]
        assert client.get(f"/media/{job_id}").status_code == 404


class TestReattach:
    def test_rejects_job_without_remote_task(self, client):
        job_id = _submit(client).json()["id"]
        r = client.post(f"/api/jobs/{job_id}/reattach")
        assert r.status_code == 400
        assert "任务号" in r.json()["error"]

    def test_accepts_job_with_remote_task(self, client, monkeypatch):
        """付过钱的任务必须能捞回来——本地一次读超时不该等于钱白花。"""
        monkeypatch.setattr(studio, "_reattach", lambda job_id: None)
        job_id = _submit(client).json()["id"]
        studio.STORE.update(job_id, status="failed", remote_task="REMOTE1")
        r = client.post(f"/api/jobs/{job_id}/reattach")
        assert r.status_code == 200
        assert r.json()["remote_task"] == "REMOTE1"

    def test_unknown_job_404(self, client):
        assert client.post("/api/jobs/nope/reattach").status_code == 404


class TestMoarkFieldMapping:
    def test_missing_required_file_is_reported(self, client, monkeypatch):
        """缺文件要在提交前给人话，不要等接口报「必传参数」。"""
        job = Job(id="j1", created_at="t", provider="moark",
                  recipe="Duix-Avatar", prompt="p", seconds=5)
        studio.STORE.add(job)
        with pytest.raises(ValueError, match="drive_video"):
            studio._run_moark("j1", job, audio_path="")

    def test_cond_video_falls_back_to_image(self, client, monkeypatch, tmp_path):
        """InfiniteTalk 要 cond_video，先用角色图兜底把链路跑通。"""
        img = tmp_path / "a.png"
        img.write_bytes(_png())
        job = Job(id="j2", created_at="t", provider="moark",
                  recipe="InfiniteTalk", prompt="p", seconds=5, image=str(img))
        studio.STORE.add(job)

        seen = {}
        import moarkclient

        monkeypatch.setattr(moarkclient, "run",
                            lambda *a, **k: seen.update(k) or
                            {"url": "http://x/v.mp4", "price": 1.0, "currency": "CNY"})
        monkeypatch.setattr(moarkclient, "download", lambda url, out: out)
        studio._run_moark("j2", job, audio_path=str(img))
        assert seen["files"]["cond_video"] == str(img)


def test_page_html_is_self_contained():
    """页面不能依赖外网 CDN：运营的机器可能连不上，或者内网隔离。"""
    assert "<!DOCTYPE html>" in studio.PAGE
    for marker in ("http://cdn", "https://cdn", "unpkg", "jsdelivr", "googleapis"):
        assert marker not in studio.PAGE


def test_page_calls_every_endpoint_it_needs():
    """页面里写死的路径必须和后端路由对得上，写错了运营只会看到一片空白。"""
    for path in ("/api/config", "/api/jobs", "/api/balance", "/media/"):
        assert path in studio.PAGE
