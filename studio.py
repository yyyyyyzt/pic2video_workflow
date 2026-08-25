#!/usr/bin/env python3
"""提示词调试台：一个网页，交给运营自己换提示词试。

和 avatar_lab.py 的分工：

    avatar_lab.py   批量、可复现、出入库素材。给工程用。
    studio.py       单条、快、随手改提示词看效果。给运营用。

设计取舍都围绕「运营不看命令行」这一条：

  提示词做成积木下拉框    不用在一大段里找那句手势
  秒数直接填             不用先想台词写多少字
  每条都显示实测扣费      让人对成本有感
  任务列表持久化在磁盘    浏览器关了、服务重启了，结果还在

启动：
    pip install -r requirements.txt
    python3 studio.py                 # 然后打开 http://127.0.0.1:8000
    python3 studio.py --host 0.0.0.0 --port 8000   # 让同事从别的机器访问
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# 这三个必须在模块级导入：本文件开了 from __future__ import annotations，
# 路由函数的类型标注会变成字符串，pydantic 解析时只在**模块全局**里找名字。
# 放进 create_app() 里会得到 PydanticUserError: is not fully defined。
try:
    from fastapi import File, Form, UploadFile
except ImportError:      # 没装 fastapi 时本模块仍应可导入，供测试检查其余逻辑
    File = Form = UploadFile = None

DATA_DIR = Path("data/studio")
JOBS_FILE = DATA_DIR / "jobs.json"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

# 海外通道的候选。label 里带上单价，运营选之前就知道贵不贵。
WAVESPEED_MODELS = [
    {"provider": "wavespeed", "recipe": "pruna-avatar",
     "label": "Pruna p-video Avatar　全表最便宜，约 $0.025 起（未实测），先拿它筛"},
    {"provider": "wavespeed", "recipe": "infinitetalk-fast",
     "label": "InfiniteTalk 快速版　约 $0.017/秒（已实测，只出 384×576）"},
    {"provider": "wavespeed", "recipe": "skyreels-std",
     "label": "SkyReels V3 标准版　约 $0.044/秒（已实测）"},
    {"provider": "wavespeed", "recipe": "soulx-flashhead",
     "label": "SoulX FlashHead　约 $0.075 起（未实测），音频支持到 30 分钟"},
    {"provider": "wavespeed", "recipe": "ltx2-19b-lipsync",
     "label": "LTX-2 19B Lipsync　约 $0.1 起（未实测），原生 1080p 可省超分"},
    {"provider": "wavespeed", "recipe": "skyreels-talking",
     "label": "SkyReels V3 Talking 19B　约 $0.15 起（未实测），带体态提示词但上限 20 秒"},
    {"provider": "wavespeed", "recipe": "hunyuan-avatar",
     "label": "腾讯混元 Avatar 720p　单价未实测，和腾讯数智人同源"},
    {"provider": "wavespeed", "recipe": "infinitetalk-720",
     "label": "InfiniteTalk 720p　单价未实测，精确 9:16"},
    {"provider": "wavespeed", "recipe": "omnihuman-15",
     "label": "字节 OmniHuman 1.5　$0.156/秒（已实测，最贵）"},
]

# 会重新生成身体、因此「坐/站」提示词有机会生效的模型。
# 其余的是从给定那一帧往下 animate，姿态改不动。
BODY_AWARE_RECIPES = {"skyreels-std", "skyreels-pro", "skyreels-talking",
                      "pruna-avatar", "omnihuman-15", "omnihuman"}

# 国内通道不写死：端点和单价都从平台自己的模型清单读，见 moarkclient.video_models()
MOARK_KIND_LABELS = {
    "audio_video2video": "数字人·口型替换（要模板视频）",
    "image2video": "图生视频",
    "multimodal_video": "多模态视频（content[]）",
    "text2video": "文生/图生视频（content[]）",
}


def studio_models() -> list[dict]:
    """调试台的模型下拉。海外写死，国内实时查平台清单。

    国内那半故意不写死：手写的端点映射已经错过一次（数字人端点猜成
    image-video-to-video，实际是 audio-video-to-video），而平台清单里就带着
    正确答案和单价。
    """
    models = list(WAVESPEED_MODELS)
    try:
        import moarkclient

        discovered = moarkclient.avatar_models()
    except Exception:                             # noqa: BLE001 查不到就只显示海外
        return models

    for entry in sorted(discovered.values(),
                        key=lambda e: (e["available"] is False, e["kind"], e["model"])):
        kind = MOARK_KIND_LABELS.get(entry["kind"], entry["kind"])
        price = f"¥{entry['price']:g}/{entry['unit']}"
        state = "" if entry["available"] is not False else "　⚠ 平台侧不可用"
        models.append({
            "provider": "moark",
            "recipe": entry["model"],
            "label": f"[国内] {entry['model']}　{kind}　{price}{state}",
            "kind": entry["kind"],
            "needs": entry["files"],
            "note": entry["note"] or entry["description"],
        })
    return models


@dataclass
class Job:
    id: str
    created_at: str
    provider: str
    recipe: str
    prompt: str
    seconds: float
    status: str = "queued"          # queued / running / done / failed
    stage: str = ""
    error: str = ""
    video: str = ""
    # 服务端任务号。本地失败了也别丢——远端还在跑，凭它能把已经付过钱的结果取回来。
    remote_task: str = ""
    cost: float = 0.0
    currency: str = ""              # 完成时按通道填 USD / CNY，未完成时留空不误导
    preset: str = ""
    blocks: dict = field(default_factory=dict)
    image: str = ""
    audio: str = ""
    template: str = ""          # 模板视频，口型替换类模型（Duix-Avatar）必需
    # 不影响成功但运营需要知道的事。stage 会被下一步覆盖，所以这类提示必须单独存。
    warnings: list = field(default_factory=list)
    script: str = ""
    voice: str = ""
    metrics: dict = field(default_factory=dict)
    elapsed: float = 0.0
    batch: str = ""             # 同一次勾选多个模型产生的任务共用一个批次号
    batch_index: int = 0


class JobStore:
    """任务列表落盘。运营会关浏览器、服务会重启，结果不能只存在内存里。"""

    def __init__(self, path: Path = JOBS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        for item in raw:
            try:
                self._jobs[item["id"]] = Job(**item)
            except TypeError:
                continue

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        self.path.write_text(
            json.dumps([asdict(j) for j in ordered], ensure_ascii=False, indent=2),
            encoding="utf-8")

    def add(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._flush()
        return job

    def update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            self._flush()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, limit: int = 60) -> list[Job]:
        return sorted(self._jobs.values(),
                      key=lambda j: j.created_at, reverse=True)[:limit]


STORE = JobStore()


def _warn(job_id: str, text: str) -> None:
    """记一条持久告警。不用 stage，因为 stage 会被下一步立刻覆盖，运营根本看不见。"""
    job = STORE.get(job_id)
    if job is None or text in job.warnings:
        return
    STORE.update(job_id, warnings=[*job.warnings, text])


def _save_upload(upload, kind: str) -> str:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix or (".png" if kind == "image" else ".mp3")
    dest = UPLOAD_DIR / f"{kind}-{uuid.uuid4().hex[:10]}{suffix}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return str(dest)


def _run_batch(job_ids: list) -> None:
    """按顺序跑一批。

    串行而不是并发，两个原因：Bronze 账号只有 2 个并发，一起冲会被限流；
    成本是靠调用前后的余额差测出来的，并发时几笔扣费会串味，算不准。
    """
    for position, job_id in enumerate(job_ids, 1):
        job = STORE.get(job_id)
        if job is None:
            continue
        STORE.update(job_id, stage=f"排队中（{position}/{len(job_ids)}）")
    for job_id in job_ids:
        _run_job(job_id)


def _run_job(job_id: str) -> None:
    """后台跑一条。所有异常都写进 job.error，不让线程静默死掉。"""
    import wsclient
    from compliance import probe

    job = STORE.get(job_id)
    if job is None:
        return
    started = time.time()
    STORE.update(job_id, status="running", stage="准备素材")

    try:
        audio_path = job.audio
        # 给了台词就先 TTS。视频长度由音频决定，所以这一步定成片时长。
        if not audio_path and job.script.strip():
            import tts

            STORE.update(job_id, stage="合成语音")
            synth, tts_cost = tts.synthesize(
                job.script, OUTPUT_DIR / job_id / "speech.mp3",
                voice=job.voice or tts.DEFAULT_VOICE)
            audio_path = str(synth)
            STORE.update(job_id, audio=audio_path, cost=round(tts_cost, 4))

        if job.provider == "moark":
            _run_moark(job_id, job, audio_path)
        else:
            _run_wavespeed(job_id, job, audio_path)

        job = STORE.get(job_id)
        summary = ""
        if job and job.video and Path(job.video).is_file():
            _attach_metrics(job_id, job.video)
            info = probe(job.video)
            summary = (f"完成 {info['width']}×{info['height']} "
                       f"{info['duration']:.1f}s")

        # 阶段文字最后写，否则会停在「算客观指标」上，看着像卡住了
        STORE.update(job_id, status="done", stage=summary or "完成",
                     elapsed=round(time.time() - started, 1))
    except Exception as exc:                      # noqa: BLE001 后台线程必须兜住一切
        STORE.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                     elapsed=round(time.time() - started, 1))


def _run_wavespeed(job_id: str, job: Job, audio_path: str) -> None:
    import wsclient
    from recipes import RECIPES, build_payload

    recipe = RECIPES.get(job.recipe)
    if recipe is None:
        raise ValueError(f"未知方案 {job.recipe}")
    if "audio" in recipe.needs and not audio_path:
        raise ValueError(f"{job.recipe} 需要音频：上传一个，或者填台词让它自动合成")

    STORE.update(job_id, stage="上传素材")
    image_url = wsclient.upload(job.image) if job.image else ""
    audio_url = wsclient.upload(audio_path) if audio_path else ""

    payload = build_payload(recipe, image_url=image_url, audio_url=audio_url,
                            prompt=job.prompt, seed=-1)

    before = None
    try:
        before = wsclient.balance()
    except wsclient.WaveSpeedError:
        pass

    STORE.update(job_id, stage="生成中")
    prediction_id = wsclient.submit(recipe.model, payload)
    STORE.update(job_id, remote_task=prediction_id)
    result = wsclient.poll(
        prediction_id,
        on_tick=lambda s, e: STORE.update(job_id, stage=f"生成中 {s} {e}s"))

    cost = 0.0
    if before is not None:
        try:
            cost = max(0.0, round(before - wsclient.balance(), 4))
        except wsclient.WaveSpeedError:
            cost = 0.0
    if cost > 0:
        wsclient.record_calibration(recipe.model, cost, job.seconds or 1.0)

    out = OUTPUT_DIR / job_id / "result.mp4"
    STORE.update(job_id, stage="下载结果")
    wsclient.download(result["outputs"][0], out)
    STORE.update(job_id, video=str(out), currency="USD",
                 cost=round(job.cost + cost, 4))


def _run_moark(job_id: str, job: Job, audio_path: str) -> None:
    import moarkclient

    endpoint = moarkclient.endpoint_for(job.recipe)
    # 口型替换类要模板视频。没上传模板时用角色图兜底把链路跑通，
    # 但会在阶段里写明，免得以为「模板视频这个参数没用」。
    template = job.template or job.image
    media = {"image": job.image, "audio": audio_path, "video": template}
    if moarkclient.uses_content(endpoint, files={k: v for k, v in media.items() if v}):
        # 多模态 / generations 图生：必填 content[]，文件走 multipart。
        # 提示词写进 JSON、图片当 image 字段，网关会把 content 丢掉。
        payload, files = moarkclient.compose_multimodal(
            prompt=job.prompt, image=job.image, audio=audio_path,
            video=job.template or "", duration=job.seconds)
        missing: list[str] = []
    else:
        files, missing = moarkclient.resolve_files(
            endpoint, image=job.image, audio=audio_path, video=template)
        payload = {"prompt": job.prompt}
    if missing:
        raise ValueError(
            f"{job.recipe} 走 {endpoint} 还缺：{', '.join(missing)}。"
            f"口型替换类模型需要上传模板视频")

    if not job.template and "ref_video" in files:
        _warn(job_id, "没有上传模板视频，用角色图兜底了。口型替换的效果主要来自"
                      "模板视频，正式跑请上传一段真人或已生成的模板")

    STORE.update(job_id, stage="提交（国内通道）")
    result = moarkclient.run(
        job.recipe, payload, files=files, endpoint=endpoint,
        on_submit=lambda tid: STORE.update(job_id, remote_task=tid),
        on_tick=lambda s, e: STORE.update(job_id, stage=f"生成中 {s} {e}s"))

    out = OUTPUT_DIR / job_id / "result.mp4"
    STORE.update(job_id, stage="下载结果")
    moarkclient.download(result["url"], out)
    STORE.update(job_id, video=str(out),
                 cost=float(result.get("price") or 0.0),
                 currency=result.get("currency") or "CNY")


def _reattach(job_id: str) -> None:
    """凭 remote_task 把已经付过钱的结果取回来。

    本地网断、进程重启、浏览器关掉都不影响服务端那边继续跑。
    没有这一步，一次读超时就等于白花一次钱。
    """
    job = STORE.get(job_id)
    if job is None or not job.remote_task:
        return
    STORE.update(job_id, status="running", stage="重新接上服务端任务", error="")
    try:
        if job.provider == "moark":
            import moarkclient

            result = moarkclient.poll(
                job.remote_task,
                on_tick=lambda s, e: STORE.update(job_id, stage=f"重连中 {s} {e}s"))
            url, cost = result["url"], float(result.get("price") or 0.0)
            currency = result.get("currency") or "CNY"
        else:
            import wsclient

            result = wsclient.poll(
                job.remote_task,
                on_tick=lambda s, e: STORE.update(job_id, stage=f"重连中 {s} {e}s"))
            url, cost, currency = result["outputs"][0], job.cost, "USD"

        out = OUTPUT_DIR / job_id / "result.mp4"
        if job.provider == "moark":
            import moarkclient

            moarkclient.download(url, out)
        else:
            import wsclient

            wsclient.download(url, out)
        STORE.update(job_id, video=str(out), cost=round(cost, 4), currency=currency)
        _attach_metrics(job_id, str(out))
        STORE.update(job_id, status="done", stage="重连成功")
    except Exception as exc:                      # noqa: BLE001
        STORE.update(job_id, status="failed",
                     error=f"重连失败 {type(exc).__name__}: {exc}")


def _attach_metrics(job_id: str, video: str) -> None:
    """顺手算一遍客观指标。缺依赖就跳过，不影响出片。"""
    try:
        import videometrics
    except ImportError:
        return
    STORE.update(job_id, stage="算客观指标")
    try:
        result = videometrics.evaluate_to_dict(video)
    except Exception as exc:                      # noqa: BLE001
        STORE.update(job_id, metrics={"available": False, "note": str(exc)})
        return
    STORE.update(job_id, metrics=result)


def create_app():
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from wsclient import load_dotenv

    load_dotenv()
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="数字人提示词调试台")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/config")
    def config() -> JSONResponse:
        import promptlib
        import tts

        return JSONResponse({
            "models": studio_models(),
            "prompt": promptlib.catalog(),
            "voices": [{"key": v.key, "label": v.label, "gender": v.gender,
                        "note": v.note} for v in tts.VOICES],
            "default_voice": tts.DEFAULT_VOICE,
            "chars_per_second": tts.CHARS_PER_SECOND,
            # 前端用它提醒：勾的模型不会重新生成身体时，「坐/站」改不动
            "body_aware": sorted(BODY_AWARE_RECIPES),
        })

    @app.get("/api/balance")
    def balance() -> JSONResponse:
        import wsclient

        out: dict = {}
        try:
            out["wavespeed"] = round(wsclient.balance(), 4)
        except Exception as exc:                  # noqa: BLE001
            out["wavespeed_error"] = str(exc)
        try:
            import moarkclient

            out["moark"] = moarkclient.quota()
            out["moark_package"] = moarkclient.package_balance()
        except Exception as exc:                  # noqa: BLE001
            out["moark_error"] = str(exc)
        return JSONResponse(out)

    @app.post("/api/jobs")
    async def create_job(
        models: str = Form(""),
        provider: str = Form("wavespeed"),
        recipe: str = Form(""),
        prompt: str = Form(""),
        seconds: float = Form(0.0),
        script: str = Form(""),
        voice: str = Form(""),
        preset: str = Form(""),
        blocks: str = Form("{}"),
        sequential: str = Form("1"),
        image: Optional[UploadFile] = File(None),
        audio: Optional[UploadFile] = File(None),
        template: Optional[UploadFile] = File(None),
    ) -> JSONResponse:
        # models 是 "provider|recipe" 的列表，勾了几个就建几条任务。
        # 兼容单个 provider/recipe 的老写法。
        picked = [m for m in (models or "").split(",") if m.strip()]
        if not picked and recipe:
            picked = [f"{provider}|{recipe}"]
        if not picked:
            return JSONResponse({"error": "至少勾一个模型"}, status_code=400)

        try:
            parsed_blocks = json.loads(blocks or "{}")
        except ValueError:
            parsed_blocks = {}

        # 素材只存一份，几条任务共用，保证横向可比
        image_path = _save_upload(image, "image") if image and image.filename else ""
        audio_path = _save_upload(audio, "audio") if audio and audio.filename else ""
        template_path = (_save_upload(template, "template")
                         if template and template.filename else "")

        if not image_path:
            return JSONResponse({"error": "需要上传角色图"}, status_code=400)
        if not audio_path and not script.strip():
            return JSONResponse(
                {"error": "需要上传音频，或者填一段台词让它自动合成"}, status_code=400)

        batch = uuid.uuid4().hex[:6] if len(picked) > 1 else ""
        created = []
        for index, item in enumerate(picked):
            item_provider, _, item_recipe = item.partition("|")
            if not item_recipe:
                item_provider, item_recipe = "wavespeed", item_provider
            job = Job(
                id=f"{datetime.now().strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
                created_at=datetime.now().isoformat(timespec="seconds"),
                provider=item_provider.strip(), recipe=item_recipe.strip(),
                prompt=prompt.strip(), seconds=float(seconds or 0),
                script=script.strip(), voice=voice, preset=preset,
                blocks=dict(parsed_blocks), batch=batch, batch_index=index,
                image=image_path, audio=audio_path, template=template_path,
            )
            STORE.add(job)
            created.append(job.id)

        if sequential in ("1", "true", "on") and len(created) > 1:
            # 串行：Bronze 账号只有 2 个并发，一起冲会被限流；而且串行时
            # 每条的实测扣费才算得准（成本是靠调用前后的余额差测的）。
            threading.Thread(target=_run_batch, args=(created,), daemon=True).start()
        else:
            for job_id in created:
                threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()

        return JSONResponse({"ids": created, "id": created[0], "batch": batch})

    @app.get("/api/jobs")
    def list_jobs() -> JSONResponse:
        return JSONResponse([asdict(j) for j in STORE.list()])

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> JSONResponse:
        job = STORE.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(asdict(job))

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str) -> JSONResponse:
        """用同样的参数重开一条。

        国内通道实测会间歇性返回 Service Temporarily Unavailable：
        同一组参数失败一次、隔几分钟再跑就成了。这种情况不自动重试（要花钱），
        但得让运营一键重来，而不是从头再填一遍表单。
        """
        old = STORE.get(job_id)
        if old is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        fresh = Job(
            id=f"{datetime.now().strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
            created_at=datetime.now().isoformat(timespec="seconds"),
            provider=old.provider, recipe=old.recipe, prompt=old.prompt,
            seconds=old.seconds, script=old.script, voice=old.voice,
            preset=old.preset, blocks=dict(old.blocks),
            image=old.image, audio=old.audio, template=old.template,
        )
        STORE.add(fresh)
        threading.Thread(target=_run_job, args=(fresh.id,), daemon=True).start()
        return JSONResponse({"id": fresh.id, "retry_of": job_id})

    @app.post("/api/jobs/{job_id}/reattach")
    def reattach(job_id: str) -> JSONResponse:
        job = STORE.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not job.remote_task:
            return JSONResponse(
                {"error": "这条没有服务端任务号，没法重连（提交时就失败了）"},
                status_code=400)
        threading.Thread(target=_reattach, args=(job_id,), daemon=True).start()
        return JSONResponse({"id": job_id, "remote_task": job.remote_task})

    @app.get("/media/{job_id}")
    def media(job_id: str):
        """下载成片。

        故意用 attachment 而不是内联播放：任务列表里内联播放会让浏览器
        自动预取每一条，几十条列表能把带宽吃光。要看就点下载。
        """
        job = STORE.get(job_id)
        if job is None or not job.video or not Path(job.video).is_file():
            return JSONResponse({"error": "not ready"}, status_code=404)
        name = f"{job.id}-{job.recipe}.mp4".replace("/", "-")
        return FileResponse(job.video, media_type="video/mp4", filename=name)

    return app


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="默认只监听本机。要让同事访问就用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("需要 fastapi + uvicorn：pip install -r requirements.txt")
        return 1

    print(f"调试台已启动 → http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数字人提示词调试台</title>
<style>
  :root { --bg:#111; --card:#1b1b1b; --line:#333; --fg:#eee; --dim:#9a9a9a; --ok:#2d5; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  header { padding:14px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; }
  .bal { color:var(--dim); font-size:13px; }
  /* 表单封顶，多余的宽度全给任务列表：表单只填一次，任务列表要一直看，
     而且卡片里文字多。不封顶的话下拉框会被拉到 700px 宽，很浪费。 */
  main { display:grid; grid-template-columns:minmax(380px,540px) minmax(360px,1fr);
         gap:0; align-items:start; }
  @media (max-width:1000px){ main{ grid-template-columns:1fr; }
                             .panel{ border-right:0; border-bottom:1px solid var(--line); } }
  .panel { padding:18px 20px; border-right:1px solid var(--line); }
  .jobs  { padding:18px 24px; }
  .jobs .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr));
                gap:12px; }

  /* 左右两列布局：标签在左、控件在右，一行一件事 */
  fieldset { border:1px solid var(--line); border-radius:10px; margin:0 0 14px; padding:12px 16px; }
  legend { padding:0 6px; color:var(--dim); font-size:13px; }
  .field { display:grid; grid-template-columns:82px 1fr; gap:10px;
           align-items:start; padding:7px 0; }
  .field > label { color:var(--dim); font-size:13px; padding-top:7px; }
  .field .note { grid-column:2; color:var(--dim); font-size:12px; margin:3px 0 0; }
  .field.stack { grid-template-columns:1fr; }
  @media (max-width:620px){ .field{ grid-template-columns:1fr; }
                            .field > label{ padding-top:0; } }

  input[type=text], input[type=number], select, textarea {
    width:100%; background:#0e0e0e; color:var(--fg); border:1px solid var(--line);
    border-radius:8px; padding:7px 10px; font:inherit; }
  textarea { min-height:70px; resize:vertical; }
  input[type=file] { width:100%; font-size:13px; color:var(--dim); }
  .two { display:grid; grid-template-columns:1fr 1fr; gap:10px; }

  button { background:var(--ok); color:#052; border:0; border-radius:8px;
           padding:11px 18px; font:600 15px/1 inherit; cursor:pointer; width:100%; }
  button:disabled { background:#444; color:#999; cursor:not-allowed; }
  button.ghost { background:#242424; color:var(--fg); border:1px solid var(--line);
                 font-weight:400; font-size:13px; padding:7px 12px; width:auto; }
  a.dl { display:inline-block; background:#164; color:#cfe; text-decoration:none;
         border-radius:8px; padding:8px 14px; font-size:13px; margin-top:8px; }
  a.dl:hover { background:#1a7a4a; }

  .preview { background:#0e0e0e; border:1px dashed var(--line); border-radius:8px;
             padding:10px; font-size:12.5px; color:#cfc; white-space:pre-wrap;
             max-height:150px; overflow:auto; }
  .models { max-height:280px; overflow:auto; border:1px solid var(--line);
            border-radius:8px; padding:6px; }
  .models .grp { color:var(--dim); font-size:12px; margin:6px 4px 3px; }
  .models label { display:flex; gap:8px; align-items:flex-start; padding:5px 6px;
                  border-radius:6px; cursor:pointer; font-size:13px; color:var(--fg); }
  .models label:hover { background:#222; }
  .models input { margin-top:3px; flex:0 0 auto; }
  .picked { color:var(--ok); font-size:12px; margin:6px 0 0; }

  .job { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:12px; margin-bottom:12px; }
  .job.failed { border-color:#833; }
  .job h3 { margin:0 0 6px; font-size:14px; display:flex; gap:6px;
            align-items:center; flex-wrap:wrap; }
  .tag { font-size:11px; background:#2a2a2a; padding:2px 8px; border-radius:999px;
         color:#ccc; font-weight:400; }
  .tag.run { background:#334; color:#bcf; }
  .tag.done{ background:#164; color:#bfd; }
  .tag.err { background:#611; color:#fbb; }
  .tag.batch { background:#432; color:#fd9; }
  .meta { color:var(--dim); font-size:12px; margin-top:5px; }
  .defect { font-size:12px; margin:6px 0 0; padding-left:16px; }
  .defect li.major { color:#f99; } .defect li.minor { color:#fc9; }
  .defect li.info { color:#8a8; }
  pre.err { color:#f99; font-size:12px; white-space:pre-wrap; margin:6px 0 0; }
  details summary { cursor:pointer; }
</style>
</head>
<body>
<header>
  <h1>数字人提示词调试台</h1>
  <span class="bal" id="bal">余额查询中…</span>
</header>

<main>
<section class="panel">
  <form id="f">
    <fieldset>
      <legend>1 · 素材</legend>
      <div class="field">
        <label>角色图</label>
        <div>
          <input type="file" name="image" accept="image/*" required>
          <p class="note">必填。正面清晰、五官无遮挡、嘴巴闭合。</p>
        </div>
      </div>
      <div class="field">
        <label>驱动音频</label>
        <div>
          <input type="file" name="audio" accept="audio/*">
          <p class="note">可选。不传就用下面的台词自动合成。</p>
        </div>
      </div>
      <div class="field">
        <label>模板视频</label>
        <div>
          <input type="file" name="template" accept="video/*">
          <p class="note">只有口型替换类模型要（国内 Duix-Avatar）。
          稳定性来自模板，拍一次可反复用，之后换台词只按秒计费。</p>
        </div>
      </div>
    </fieldset>

    <fieldset>
      <legend>2 · 台词与时长</legend>
      <div class="field">
        <label>目标秒数</label>
        <div class="two">
          <input type="number" id="seconds" name="seconds" value="10" min="1" max="600" step="1">
          <select id="voice" name="voice"></select>
        </div>
      </div>
      <div class="field">
        <label>台词</label>
        <div>
          <textarea id="script" name="script" placeholder="填台词，或者上面直接传音频"></textarea>
          <p class="note" id="charhint"></p>
          <button type="button" class="ghost" id="fit">按秒数裁剪台词</button>
        </div>
      </div>
    </fieldset>

    <fieldset>
      <legend>3 · 提示词</legend>
      <div class="field">
        <label>预设</label>
        <div>
          <select id="preset"></select>
          <p class="note" id="presetnote"></p>
        </div>
      </div>
      <div id="blocks"></div>
      <div class="field">
        <label>额外补充</label>
        <textarea id="extra" placeholder="想加的话写在这里，会拼到最后"></textarea>
      </div>
      <div class="field stack">
        <label>最终提示词</label>
        <div class="preview" id="preview"></div>
        <button type="button" class="ghost" id="edit">改成手动编辑</button>
        <textarea id="manual" style="display:none" placeholder="手动提示词"></textarea>
      </div>
    </fieldset>

    <fieldset>
      <legend>4 · 模型（可多选）</legend>
      <div class="field stack">
        <div class="models" id="models"></div>
        <p class="picked" id="picked"></p>
        <label style="display:flex;gap:8px;align-items:center;color:var(--dim)">
          <input type="checkbox" id="sequential" checked>
          按顺序一个个跑（并发会被限流，串行的实测扣费也才算得准）
        </label>
      </div>
    </fieldset>

    <button type="submit" id="go">生成</button>
    <p class="note" id="err" style="color:#f99"></p>
  </form>
</section>

<section class="jobs">
  <p class="meta" id="jobhint"></p>
  <div id="list"></div>
</section>
</main>

<script>
let CFG = null, MANUAL = false;
const $ = (id) => document.getElementById(id);

async function boot() {
  CFG = await (await fetch("/api/config")).json();

  const voice = $("voice");
  for (const v of CFG.voices) {
    const o = document.createElement("option");
    o.value = v.key; o.textContent = v.label;
    if (v.key === CFG.default_voice) o.selected = true;
    voice.appendChild(o);
  }

  // 模型多选。国内和海外分组显示，勾几个就建几条任务。
  const holder = $("models");
  let lastProvider = null;
  for (const m of CFG.models) {
    if (m.provider !== lastProvider) {
      const h = document.createElement("div");
      h.className = "grp";
      h.textContent = m.provider === "moark" ? "国内通道（模力方舟）" : "海外通道（WaveSpeed）";
      holder.appendChild(h);
      lastProvider = m.provider;
    }
    const id = m.provider + "|" + m.recipe;
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${id}"> <span>${m.label}</span>`;
    label.querySelector("input").addEventListener("change", updatePicked);
    holder.appendChild(label);
  }
  // 默认勾最便宜的那个
  const first = holder.querySelector("input[type=checkbox]");
  if (first) { first.checked = true; }

  const preset = $("preset");
  for (const [key, spec] of Object.entries(CFG.prompt.presets)) {
    const o = document.createElement("option");
    o.value = key; o.textContent = spec.label;
    preset.appendChild(o);
  }
  preset.addEventListener("change", applyPreset);

  const groups = $("blocks");
  for (const [group, spec] of Object.entries(CFG.prompt.groups)) {
    const row = document.createElement("div");
    row.className = "field";
    const lab = document.createElement("label");
    lab.textContent = spec.label;
    const wrap = document.createElement("div");
    const sel = document.createElement("select");
    sel.dataset.group = group; sel.id = "g-" + group;
    for (const opt of spec.options) {
      const o = document.createElement("option");
      o.value = opt.key; o.textContent = opt.label;
      o.title = opt.note || opt.text;
      sel.appendChild(o);
    }
    sel.addEventListener("change", render);
    wrap.appendChild(sel);
    if (spec.hint) {
      const hint = document.createElement("p");
      hint.className = "note"; hint.textContent = spec.hint;
      wrap.appendChild(hint);
    }
    row.appendChild(lab); row.appendChild(wrap);
    groups.appendChild(row);
  }

  $("extra").addEventListener("input", render);
  $("seconds").addEventListener("input", updateChars);
  $("script").addEventListener("input", updateChars);
  applyPreset();
  updateChars();
  updatePicked();
  refreshBalance();
  poll();
}

function pickedModels() {
  return [...document.querySelectorAll("#models input:checked")].map(i => i.value);
}

function updatePicked() {
  const n = pickedModels().length;
  $("picked").textContent = n ? `已勾 ${n} 个模型，会建 ${n} 条任务` : "还没勾模型";
  // 「坐/站」只在会重新生成身体的模型上有机会生效
  const bodyAware = pickedModels().some(v => (CFG.body_aware || []).includes(v.split("|")[1]));
  const sel = $("g-body");
  if (sel && sel.value !== "keep" && !bodyAware) {
    $("picked").textContent += "　注意：勾选的模型都不会重新生成身体，坐/站改不动";
  }
}

function applyPreset() {
  const spec = CFG.prompt.presets[$("preset").value];
  $("presetnote").textContent = spec.note || "";
  for (const [group, key] of Object.entries(spec.blocks)) {
    const sel = $("g-" + group);
    if (sel) sel.value = key;
  }
  render();
}

function currentBlocks() {
  const out = {};
  for (const group of Object.keys(CFG.prompt.groups)) {
    const sel = $("g-" + group);
    if (sel) out[group] = sel.value;
  }
  return out;
}

/* 前端按同样的顺序拼一遍，让运营在点生成之前就看到确切会发出去的文字。
   后端不再重新拼装，直接用这段，避免两边算出来不一样。 */
function render() {
  if (MANUAL) return;
  const g = CFG.prompt.groups, b = currentBlocks();
  const order = ["body", "posture", "gesture", "expression", "speech", "negative"];
  const parts = [CFG.prompt.base];
  for (const group of order) {
    const opt = (g[group]?.options || []).find(o => o.key === b[group]);
    if (opt && opt.text.trim()) parts.push(opt.text.trim());
  }
  const extra = $("extra").value.trim();
  if (extra) parts.push(extra);
  $("preview").textContent = parts.join(" ");
  updatePicked();
}

function promptText() {
  return MANUAL ? $("manual").value.trim() : $("preview").textContent.trim();
}

$("edit").addEventListener("click", () => {
  MANUAL = !MANUAL;
  $("manual").style.display = MANUAL ? "block" : "none";
  $("preview").style.display = MANUAL ? "none" : "block";
  if (MANUAL && !$("manual").value) $("manual").value = $("preview").textContent;
  $("edit").textContent = MANUAL ? "回到积木模式" : "改成手动编辑";
});

function updateChars() {
  const cps = CFG.chars_per_second, want = Number($("seconds").value || 0);
  const have = $("script").value.replace(/\s/g, "").length;
  const need = Math.round(want * cps);
  $("charhint").textContent =
    `${want} 秒约需 ${need} 字（每秒 ${cps} 字，实测值）。当前台词 ${have} 字` +
    (have ? `，约 ${(have / cps).toFixed(0)} 秒` : "");
}

/* 按秒数裁剪：在句号处断，不在句子中间切断，否则 TTS 读出来会很怪 */
$("fit").addEventListener("click", () => {
  const cps = CFG.chars_per_second, want = Number($("seconds").value || 0);
  const budget = Math.round(want * cps);
  const text = $("script").value.trim();
  if (!text || !budget) return;
  const sentences = text.split(/(?<=[。！？])/);
  let out = "", used = 0;
  for (const s of sentences) {
    const n = s.replace(/\s/g, "").length;
    if (used + n > budget && out) break;
    out += s; used += n;
  }
  $("script").value = out || text.slice(0, budget);
  updateChars();
});

$("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("err").textContent = "";
  const picked = pickedModels();
  if (!picked.length) { $("err").textContent = "至少勾一个模型"; return; }

  const form = new FormData($("f"));
  form.set("models", picked.join(","));
  form.set("prompt", promptText());
  form.set("preset", $("preset").value);
  form.set("blocks", JSON.stringify(currentBlocks()));
  form.set("sequential", $("sequential").checked ? "1" : "0");

  $("go").disabled = true; $("go").textContent = `提交中…（${picked.length} 条）`;
  try {
    const resp = await fetch("/api/jobs", { method: "POST", body: form });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "提交失败");
    poll();
  } catch (ex) {
    $("err").textContent = ex.message;
  } finally {
    $("go").disabled = false; $("go").textContent = "生成";
  }
});

async function refreshBalance() {
  try {
    const b = await (await fetch("/api/balance")).json();
    const bits = [];
    if (b.wavespeed !== undefined) bits.push(`WaveSpeed $${b.wavespeed}`);
    if (b.wavespeed_error) bits.push("WaveSpeed 未配置");
    if (b.moark_package) bits.push(`国内资源包 ¥${b.moark_package.balance}`);
    if (b.moark && b.moark.available !== undefined)
      bits.push(`并发 ${b.moark.available}/${b.moark.max_concurrency}`);
    $("bal").textContent = bits.join("　") || "余额不可用";
  } catch { $("bal").textContent = "余额不可用"; }
}

function jobCard(j) {
  const cls = j.status === "failed" ? "err" : j.status === "done" ? "done" : "run";
  const money = j.cost ? `${j.currency === "CNY" ? "¥" : "$"}${j.cost}` : "-";
  const defects = ((j.metrics && j.metrics.defects) || []).map(d =>
    `<li class="${d.severity}">${d.dimension}/${d.code} ${d.detail}</li>`).join("");
  const counts = j.metrics && j.metrics.available
    ? `${j.metrics.major_count} 重 / ${j.metrics.minor_count} 轻` : "";
  const recoverable = j.status === "failed" && j.remote_task;
  /* 不做内联播放：列表里几十条会被浏览器自动预取，带宽扛不住。要看就下载。 */
  return `<div class="job ${j.status}">
    <h3>${j.id}
      <span class="tag ${cls}">${j.status}</span>
      <span class="tag">${j.recipe}</span>
      <span class="tag">${j.seconds || "?"}s</span>
      ${j.batch ? `<span class="tag batch">批次 ${j.batch}</span>` : ""}
      ${j.preset ? `<span class="tag">${j.preset}</span>` : ""}
      ${counts ? `<span class="tag">${counts}</span>` : ""}
    </h3>
    <div class="meta">${j.stage || ""}　${money}${j.elapsed ? "　耗时 " + j.elapsed + "s" : ""}</div>
    ${j.video ? `<a class="dl" href="/media/${j.id}" download>下载成片</a>` : ""}
    ${defects ? `<ul class="defect">${defects}</ul>` : ""}
    ${(j.warnings || []).map(w => `<p class="meta" style="color:#fc9">注意：${w}</p>`).join("")}
    ${j.error ? `<pre class="err">${j.error}</pre>` : ""}
    ${recoverable ? `<button class="ghost" onclick="reattach('${j.id}')">
        重连取回（${j.remote_task}）</button>` : ""}
    ${j.status === "failed" ? `<button class="ghost" onclick="retry('${j.id}')">
        用同样的参数重试</button>` : ""}
    <details><summary class="meta">提示词</summary><div class="preview">${j.prompt || "(空)"}</div></details>
  </div>`;
}

async function retry(id) {
  const r = await fetch(`/api/jobs/${id}/retry`, { method: "POST" });
  if (!r.ok) alert((await r.json()).error || "重试失败");
  poll();
}

async function reattach(id) {
  const r = await fetch(`/api/jobs/${id}/reattach`, { method: "POST" });
  if (!r.ok) alert((await r.json()).error || "重连失败");
  poll();
}

async function poll() {
  try {
    const jobs = await (await fetch("/api/jobs")).json();
    const running = jobs.filter(j => j.status === "queued" || j.status === "running").length;
    $("jobhint").textContent = jobs.length
      ? `共 ${jobs.length} 条，${running} 条在跑。同批次的任务参数完全一样，只有模型不同，可以直接对比。`
      : "";
    $("list").innerHTML = jobs.length
      ? `<div class="grid">${jobs.map(jobCard).join("")}</div>`
      : '<p class="meta">还没有任务。左边填好、勾上模型，点生成。</p>';
    if (running) refreshBalance();
  } catch {}
  setTimeout(poll, 4000);
}

boot();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
