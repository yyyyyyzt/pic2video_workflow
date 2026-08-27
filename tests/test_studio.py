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
    data = {"models": "wavespeed|skyreels-std",
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

    def test_nan_metrics_flush_as_null(self, tmp_path):
        """检不到脸时 face_px_median 是 nan。落盘必须写成 null，否则下次
        再读没问题，但 /api/jobs 用 Starlette JSONResponse 会 500。"""
        path = tmp_path / "jobs.json"
        store = JobStore(path)
        store.add(Job(id="a", created_at="t", provider="w", recipe="r",
                      prompt="", seconds=1,
                      metrics={"合理性": {"face_px_median": float("nan")}}))
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw[0]["metrics"]["合理性"]["face_px_median"] is None

    def test_legacy_nan_on_disk_is_rewritten_null(self, tmp_path):
        """线上 jobs.json 已经用 Python json 写出过 NaN。下次落盘要洗成 null。"""
        path = tmp_path / "jobs.json"
        path.write_text(json.dumps([{
            "id": "a", "created_at": "t", "provider": "w", "recipe": "r",
            "prompt": "", "seconds": 1,
            "metrics": {"合理性": {"face_px_median": float("nan")}},
        }]), encoding="utf-8")
        store = JobStore(path)
        store.update("a", status="done")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw[0]["metrics"]["合理性"]["face_px_median"] is None
        assert raw[0]["status"] == "done"


class TestConfigEndpoint:
    def test_exposes_everything_the_page_needs(self, client):
        data = client.get("/api/config").json()
        assert set(data) == {"models", "prompt", "voices", "default_voice",
                             "chars_per_second", "body_aware"}
        assert data["models"] and data["voices"]

    def test_model_entries_have_provider_and_recipe(self, client):
        for m in client.get("/api/config").json()["models"]:
            assert m["provider"] in ("wavespeed", "moark")
            assert m["recipe"] and m["label"]

    def test_wavespeed_entries_reference_real_recipes(self, client):
        from recipes import RECIPES

        for m in client.get("/api/config").json()["models"]:
            if m["provider"] == "wavespeed":
                assert m["recipe"] in RECIPES, f"{m['recipe']} 不存在"

    def test_moark_entries_come_from_catalog(self, monkeypatch):
        """国内那半是查平台清单来的，不再手写端点——手写过一次就猜错了。"""
        import moarkclient

        monkeypatch.setattr(moarkclient, "model_details", lambda **kw: [
            {"id": "Duix-Avatar", "description": "d",
             "operations": [{"type": "audio_video2video", "name": "数字人生成",
                             "path": "v1/async/videos/audio-video-to-video",
                             "price": "0.01", "unit_tag": {"name": "秒"}}]}])
        entries = [m for m in studio.studio_models() if m["provider"] == "moark"]
        assert len(entries) == 1
        assert entries[0]["recipe"] == "Duix-Avatar"
        assert "¥0.01/秒" in entries[0]["label"]

    def test_moark_lookup_failure_still_lists_wavespeed(self, monkeypatch):
        """查不到国内清单时，页面不能整个空掉。"""
        import moarkclient

        def boom(**kw):
            raise moarkclient.MoarkError("没配 token")

        monkeypatch.setattr(moarkclient, "model_details", boom)
        models = studio.studio_models()
        assert models and all(m["provider"] == "wavespeed" for m in models)

    def test_labels_mention_price(self, client):
        """运营选模型之前就该知道贵不贵——OmniHuman 一条 80 秒就是 $12.6。"""
        for m in client.get("/api/config").json()["models"]:
            if m["provider"] == "wavespeed":
                assert "$" in m["label"] or "未实测" in m["label"]


class TestValidation:
    def test_missing_image_returns_400_with_chinese(self, client):
        r = client.post("/api/jobs", data={"models": "wavespeed|skyreels-std"})
        assert r.status_code == 400
        assert "角色图" in r.json()["error"]

    def test_missing_audio_and_script_returns_400(self, client):
        r = client.post("/api/jobs",
                        data={"models": "wavespeed|skyreels-std", "prompt": "p"},
                        files={"image": ("f.png", _png(), "image/png")})
        assert r.status_code == 400
        assert "台词" in r.json()["error"]

    def test_script_only_is_accepted(self, client):
        assert _submit(client).status_code == 200

    def test_audio_only_is_accepted(self, client):
        r = client.post("/api/jobs",
                        data={"models": "wavespeed|skyreels-std", "prompt": "p",
                              "script": ""},
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

    def test_nan_metrics_do_not_break_job_list(self, client):
        """复现：pruna-avatar 全程检不到脸，face_px_median 是 nan，
        GET /api/jobs 整页 500。已落盘的脏任务也必须能列出来。"""
        job_id = _submit(client).json()["id"]
        studio.STORE.update(job_id, metrics={
            "available": True,
            "metrics": {"合理性": {"face_px_median": float("nan"),
                                   "face_detect_rate": 0.0}},
        })
        r = client.get("/api/jobs")
        assert r.status_code == 200
        job = next(j for j in r.json() if j["id"] == job_id)
        assert job["metrics"]["metrics"]["合理性"]["face_px_median"] is None
        detail = client.get(f"/api/jobs/{job_id}")
        assert detail.status_code == 200
        assert detail.json()["metrics"]["metrics"]["合理性"]["face_px_median"] is None

    def test_media_not_ready_404(self, client):
        job_id = _submit(client).json()["id"]
        assert client.get(f"/media/{job_id}").status_code == 404


class TestRetry:
    def test_creates_new_job_with_same_params(self, client):
        """国内通道会间歇性报 Service Temporarily Unavailable，
        同样参数隔几分钟再跑就成。一键重来，不用重填表单。"""
        job_id = _submit(client, prompt="手指点数").json()["id"]
        studio.STORE.update(job_id, status="failed", error="Service Temporarily Unavailable")
        r = client.post(f"/api/jobs/{job_id}/retry")
        assert r.status_code == 200
        fresh = studio.STORE.get(r.json()["id"])
        assert fresh.id != job_id
        assert fresh.prompt == "手指点数"
        assert fresh.image == studio.STORE.get(job_id).image

    def test_reuses_uploaded_files(self, client):
        job_id = _submit(client).json()["id"]
        studio.STORE.update(job_id, status="failed", template="/tmp/t.mp4")
        new_id = client.post(f"/api/jobs/{job_id}/retry").json()["id"]
        assert studio.STORE.get(new_id).template == "/tmp/t.mp4"

    def test_unknown_job_404(self, client):
        assert client.post("/api/jobs/nope/retry").status_code == 404


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
    @pytest.fixture(autouse=True)
    def _catalog(self, monkeypatch):
        import moarkclient

        monkeypatch.setattr(moarkclient, "model_details", lambda **kw: [
            {"id": "Duix-Avatar", "operations": [
                {"type": "audio_video2video", "path":
                 "v1/async/videos/audio-video-to-video", "price": "0.01"}]},
            {"id": "InfiniteTalk", "operations": [
                {"type": "image2video", "path":
                 "v1/async/videos/image-to-video", "price": "0.5"}]},
            {"id": "seedance-2.0", "operations": [
                {"type": "multimodal_video", "path":
                 "v1/async/videos/generations/multimodal", "price": "51"}]},
        ])

    def test_missing_required_file_is_reported(self, client):
        """缺文件要在提交前给人话，不要等接口报「必传参数」。"""
        job = Job(id="j1", created_at="t", provider="moark",
                  recipe="Duix-Avatar", prompt="p", seconds=5)
        studio.STORE.add(job)
        with pytest.raises(ValueError, match="ref_audio"):
            studio._run_moark("j1", job, audio_path="")

    def test_error_names_the_template_video(self, client, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(_png())
        job = Job(id="j1b", created_at="t", provider="moark",
                  recipe="Duix-Avatar", prompt="p", seconds=5)
        studio.STORE.add(job)
        with pytest.raises(ValueError, match="模板视频"):
            studio._run_moark("j1b", job, audio_path=str(img))

    def test_avatar_endpoint_uses_ref_fields(self, client, monkeypatch, tmp_path):
        """实测数字人端点要 ref_audio + ref_video，不是 drive_video。"""
        media = tmp_path / "a.mp4"
        media.write_bytes(b"x")
        job = Job(id="j2", created_at="t", provider="moark",
                  recipe="Duix-Avatar", prompt="p", seconds=5,
                  image=str(media), template=str(media))
        studio.STORE.add(job)

        seen = {}
        import moarkclient

        monkeypatch.setattr(moarkclient, "run",
                            lambda *a, **k: seen.update(k) or
                            {"url": "http://x/v.mp4", "price": 0.6, "currency": "CNY"})
        monkeypatch.setattr(moarkclient, "download", lambda url, out: out)
        studio._run_moark("j2", job, audio_path=str(media))
        assert set(seen["files"]) == {"ref_audio", "ref_video"}
        assert seen["endpoint"] == moarkclient.EP_AUDIO_VIDEO_TO_VIDEO

    def test_template_falls_back_to_image(self, client, monkeypatch, tmp_path):
        """没上传模板视频时用角色图兜底，先把链路跑通。"""
        img = tmp_path / "a.png"
        img.write_bytes(_png())
        job = Job(id="j3", created_at="t", provider="moark",
                  recipe="Duix-Avatar", prompt="p", seconds=5, image=str(img))
        studio.STORE.add(job)

        seen = {}
        import moarkclient

        monkeypatch.setattr(moarkclient, "run",
                            lambda *a, **k: seen.update(k) or
                            {"url": "u", "price": 0.6, "currency": "CNY"})
        monkeypatch.setattr(moarkclient, "download", lambda url, out: out)
        studio._run_moark("j3", job, audio_path=str(img))
        assert seen["files"]["ref_video"] == str(img)
        # 用持久的 warnings，不能用 stage —— stage 会被下一步覆盖
        assert any("模板视频" in w for w in studio.STORE.get("j3").warnings)

    def test_multimodal_sends_content_not_prompt_json(self, client, monkeypatch, tmp_path):
        """国内多模态端点缺 content 会 HTTP 400。角色图走 files，content[] 走 multipart。"""
        img = tmp_path / "a.png"
        img.write_bytes(_png())
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"ID3fake")
        job = Job(id="j4", created_at="t", provider="moark",
                  recipe="seedance-2.0", prompt="人在说话", seconds=5,
                  image=str(img))
        studio.STORE.add(job)

        seen = {}
        import moarkclient

        def fake_run(*a, **k):
            seen.update(k)
            if len(a) > 1:
                seen["payload"] = a[1]
            return {"url": "u", "price": 1.0, "currency": "CNY"}

        monkeypatch.setattr(moarkclient, "run", fake_run)
        monkeypatch.setattr(moarkclient, "download", lambda url, out: out)
        studio._run_moark("j4", job, audio_path=str(audio))
        assert seen["endpoint"] == moarkclient.EP_MULTIMODAL
        assert "image" in seen["files"]
        assert "audio" in seen["files"]
        content = seen["payload"]["content"]
        assert content[0] == {"type": "text", "text": "人在说话"}
        assert any(p.get("type") == "image_url" for p in content)
        assert any(p.get("type") == "audio_url" for p in content)


def test_page_html_is_self_contained():
    """页面不能依赖外网 CDN：运营的机器可能连不上，或者内网隔离。"""
    assert "<!DOCTYPE html>" in studio.PAGE
    for marker in ("http://cdn", "https://cdn", "unpkg", "jsdelivr", "googleapis"):
        assert marker not in studio.PAGE


def test_page_calls_every_endpoint_it_needs():
    """页面里写死的路径必须和后端路由对得上，写错了运营只会看到一片空白。"""
    for path in ("/api/config", "/api/jobs", "/api/balance", "/media/"):
        assert path in studio.PAGE


class TestMultiModel:
    """一次勾多个模型 → 建多条任务，素材共用，参数完全一致才能横向比。"""

    def test_creates_one_job_per_model(self, client):
        r = _submit(client, models="wavespeed|skyreels-std,wavespeed|pruna-avatar,"
                                   "wavespeed|soulx-flashhead")
        assert r.status_code == 200
        assert len(r.json()["ids"]) == 3

    def test_jobs_share_one_batch_id(self, client):
        ids = _submit(client, models="wavespeed|skyreels-std,"
                                     "wavespeed|pruna-avatar").json()["ids"]
        batches = {studio.STORE.get(i).batch for i in ids}
        assert len(batches) == 1 and batches != {""}

    def test_single_model_has_no_batch_id(self, client):
        """只勾一个就不该显示批次标签。"""
        job_id = _submit(client).json()["id"]
        assert studio.STORE.get(job_id).batch == ""

    def test_uploads_are_shared_not_duplicated(self, client):
        ids = _submit(client, models="wavespeed|skyreels-std,"
                                     "wavespeed|pruna-avatar").json()["ids"]
        images = {studio.STORE.get(i).image for i in ids}
        assert len(images) == 1, "同批任务必须用同一份素材，否则不可比"

    def test_prompt_and_blocks_identical_across_batch(self, client):
        blocks = json.dumps({"body": "to-sitting", "gesture": "clasped"})
        ids = _submit(client, models="wavespeed|skyreels-std,wavespeed|pruna-avatar",
                      prompt="同一句", blocks=blocks).json()["ids"]
        jobs = [studio.STORE.get(i) for i in ids]
        assert len({j.prompt for j in jobs}) == 1
        assert len({json.dumps(j.blocks, sort_keys=True) for j in jobs}) == 1

    def test_batch_index_is_ordered(self, client):
        ids = _submit(client, models="wavespeed|a,wavespeed|b,wavespeed|c").json()["ids"]
        assert [studio.STORE.get(i).batch_index for i in ids] == [0, 1, 2]

    def test_recipe_order_preserved(self, client):
        ids = _submit(client, models="wavespeed|pruna-avatar,"
                                     "wavespeed|skyreels-std").json()["ids"]
        assert [studio.STORE.get(i).recipe for i in ids] == \
            ["pruna-avatar", "skyreels-std"]

    def test_no_models_selected_is_400(self, client):
        r = client.post("/api/jobs", data={"models": ""},
                        files={"image": ("f.png", _png(), "image/png")})
        assert r.status_code == 400
        assert "模型" in r.json()["error"]

    def test_bare_recipe_defaults_to_wavespeed(self, client):
        job_id = _submit(client, models="skyreels-std").json()["id"]
        assert studio.STORE.get(job_id).provider == "wavespeed"

    def test_legacy_provider_recipe_fields_still_work(self, client):
        r = client.post("/api/jobs",
                        data={"provider": "wavespeed", "recipe": "skyreels-std",
                              "script": "台词"},
                        files={"image": ("f.png", _png(), "image/png")})
        assert r.status_code == 200

    def test_sequential_marks_queue_positions(self, client, monkeypatch):
        """串行时要让人看出还有几条在排队，否则像卡住了。"""
        monkeypatch.setattr(studio, "_run_job", lambda job_id: None)
        ids = _submit(client, models="wavespeed|a,wavespeed|b,wavespeed|c",
                      sequential="1").json()["ids"]
        studio._run_batch(ids)
        assert "3" in studio.STORE.get(ids[2]).stage


class TestDownloadInsteadOfPreview:
    def test_page_has_no_inline_video(self):
        """任务列表内联 <video> 会让浏览器自动预取每一条，带宽扛不住。"""
        assert "<video" not in studio.PAGE

    def test_page_offers_download_link(self):
        assert 'class="dl"' in studio.PAGE and "下载成片" in studio.PAGE

    def test_media_sets_attachment_filename(self, client, tmp_path):
        job_id = _submit(client).json()["id"]
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        studio.STORE.update(job_id, video=str(video))
        r = client.get(f"/media/{job_id}")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")


class TestTwoColumnLayout:
    def test_uses_label_control_grid(self):
        """每行「标签在左、控件在右」。断言结构而不是具体像素，免得调宽度就红。"""
        import re

        assert ".field { display:grid;" in studio.PAGE
        assert re.search(r"\.field \{ display:grid; grid-template-columns:\d+px 1fr",
                         studio.PAGE)

    def test_collapses_on_narrow_screens(self):
        assert "@media (max-width:620px)" in studio.PAGE

    def test_form_column_is_capped(self):
        """表单封顶，多余宽度给任务列表——不封顶下拉框会被拉到 700px。"""
        assert "minmax(380px,540px)" in studio.PAGE


class TestBodyModeHint:
    def test_config_lists_body_aware_recipes(self, client):
        aware = client.get("/api/config").json()["body_aware"]
        assert "skyreels-std" in aware
        # 纯口型替换类不会重新生成身体
        assert "lipsync-2-pro" not in aware

    def test_page_warns_when_model_cannot_change_posture(self):
        assert "坐/站改不动" in studio.PAGE
