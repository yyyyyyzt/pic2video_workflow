#!/usr/bin/env python3
"""可灵数字人 Web 控制台 —— 上传图片/音频（或台词转配音）、看任务进度。

启动：
    python3 server.py                 # http://127.0.0.1:8000
    python3 server.py --port 9000 --host 0.0.0.0

无登录鉴权，仅供本机/内网自用。任务串行执行（避免打爆 API 并发额度），
状态持久化到 data/jobs.json，重启后仍能看到历史任务并继续轮询未完成的。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import klingclient as kc

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
JOBS_FILE = DATA_DIR / "jobs.json"
WEB_DIR = ROOT / "web"

# 任务阶段 → 进度百分比。数字人接口不返回细粒度百分比，
# 所以用阶段推进 + 已耗时来表达进度，比假装有百分比更诚实。
STAGES = {
    "queued": (0, "排队等待"),
    "tts": (8, "台词转配音"),
    "submitting": (15, "提交任务"),
    "generating": (25, "模型生成中"),
    "downloading": (85, "下载成片"),
    "stabilizing": (92, "抖动后处理"),
    "done": (100, "完成"),
    "failed": (100, "失败"),
}

app = FastAPI(title="可灵数字人控制台", docs_url="/api/docs")

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_queue: Queue[str] = Queue()


# --------------------------------------------------------------------------- 持久化


def load_jobs() -> None:
    if not JOBS_FILE.is_file():
        return
    try:
        stored = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    with _lock:
        _jobs.update(stored)
        # 上次进程被杀时留下的“进行中”任务：有 task_id 的重新入队继续轮询
        for job_id, job in _jobs.items():
            if job.get("stage") not in ("done", "failed"):
                if job.get("task_id"):
                    job["stage"] = "generating"
                    _queue.put(job_id)
                else:
                    job["stage"] = "failed"
                    job["error"] = "服务重启，任务未提交成功"


def save_jobs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        snapshot = json.dumps(_jobs, ensure_ascii=False, indent=2)
    tmp = JOBS_FILE.with_suffix(".json.tmp")
    tmp.write_text(snapshot, encoding="utf-8")
    tmp.replace(JOBS_FILE)


def update_job(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        stage = job.get("stage", "queued")
        job["percent"], job["stage_label"] = STAGES.get(stage, (0, stage))
        job["updated_at"] = time.time()
    save_jobs()


def log(job_id: str, message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.setdefault("logs", []).append(f"[{stamp}] {message}")
        job["logs"] = job["logs"][-60:]
    save_jobs()
    print(f"[{job_id[:8]}] {message}", flush=True)


# --------------------------------------------------------------------------- worker


def process_job(job_id: str) -> None:
    with _lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return

    try:
        token_factory = kc.resolve_auth()[1]
        audio_id = job.get("audio_id")
        audio_path = job.get("audio_path")

        # 断点续跑：已有 task_id 说明只需继续轮询
        task_id = job.get("task_id")
        if not task_id:
            if not audio_path and not audio_id:
                update_job(job_id, stage="tts")
                log(job_id, f"台词 {len(job['script'])} 字 → TTS（音色 {job['voice']}，语速 {job['voice_speed']}）")
                tts = kc.synthesize_speech(token_factory(), job["script"], job["voice"],
                                           job["voice_language"], float(job["voice_speed"]))
                audio_id = tts["audio_id"]
                update_job(job_id, audio_id=audio_id, audio_duration=tts["duration"],
                           audio_preview=tts["url"])
                log(job_id, f"配音完成 {tts['duration']:.1f} 秒")
                if tts["duration"] > kc.AUDIO_MAX_SECONDS:
                    log(job_id, f"注意：音频超过文档标注的 {kc.AUDIO_MAX_SECONDS} 秒上限，接口可能拒绝")

            update_job(job_id, stage="submitting")
            task_id = kc.create_avatar_task(
                token_factory(), image=job["image_path"], audio=audio_path,
                audio_id=audio_id, prompt=job.get("prompt", ""), mode=job.get("mode", "std"),
            )
            update_job(job_id, task_id=task_id)
            log(job_id, f"任务已提交 task_id={task_id}")

        update_job(job_id, stage="generating")
        deadline = time.time() + int(job.get("timeout", 7200))
        video_url = ""
        while time.time() < deadline:
            task = kc.get_avatar_task(token_factory(), task_id)
            with _lock:
                current = _jobs.get(job_id, {})
                changed = current.get("api_status") != task["status"]
            update_job(job_id, api_status=task["status"])
            if changed:
                log(job_id, f"状态: {task['status']}")

            if task["status"] == "succeed":
                if not task["videos"]:
                    raise kc.KlingError("任务成功但未返回视频地址")
                video_url = task["videos"][0]["url"]
                update_job(job_id, video_duration=task["videos"][0].get("duration", ""))
                break
            if task["status"] == "failed":
                raise kc.KlingError(f"生成失败: {task['message'] or '(接口未给原因)'}")
            time.sleep(int(job.get("poll_interval", 15)))
        else:
            raise kc.KlingError("等待超时，任务可能仍在进行，可稍后刷新或用 task_id 手动查询")

        update_job(job_id, stage="downloading")
        log(job_id, "下载成片")
        output = OUTPUT_DIR / f"{job_id}.mp4"
        kc.download(video_url, output)
        size_mb = output.stat().st_size / 1024 / 1024
        log(job_id, f"已保存 {size_mb:.1f}MB")
        update_job(job_id, output_file=output.name, size_mb=round(size_mb, 1))

        method = job.get("stabilize")
        if method:
            update_job(job_id, stage="stabilizing")
            log(job_id, f"后处理 method={method}")
            raw = OUTPUT_DIR / f"{job_id}_raw.mp4"
            output.replace(raw)
            cmd = [sys.executable, str(ROOT / "stabilize.py"), str(raw),
                   "-o", str(output), "--method", method]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raw.replace(output)
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                log(job_id, "后处理失败，已保留原始成片：" + " / ".join(tail))
            else:
                update_job(job_id, raw_file=raw.name)
                for line in (proc.stdout or "").strip().splitlines()[-4:]:
                    if line.strip():
                        log(job_id, line.strip())

        update_job(job_id, stage="done", finished_at=time.time())
        log(job_id, "任务完成")

    except kc.KlingError as exc:
        update_job(job_id, stage="failed", error=str(exc), finished_at=time.time())
        log(job_id, f"失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - worker 线程必须兜住一切，否则整条队列停摆
        update_job(job_id, stage="failed", error=f"{type(exc).__name__}: {exc}",
                   finished_at=time.time())
        log(job_id, f"异常：{type(exc).__name__}: {exc}")


def worker_loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            process_job(job_id)
        finally:
            _queue.task_done()


# --------------------------------------------------------------------------- API


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(500, "缺少 web/index.html")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/meta")
def meta() -> dict:
    """前端启动时拉取：音色列表、提示词模板、鉴权是否就绪。"""
    try:
        auth_mode, _ = kc.resolve_auth()
        auth_ok, auth_error = True, ""
    except kc.KlingError as exc:
        auth_mode, auth_ok, auth_error = "", False, str(exc)

    templates = []
    prompts_dir = ROOT / "prompts"
    if prompts_dir.is_dir():
        for path in sorted(prompts_dir.glob("*.txt")):
            lines = [ln.rstrip() for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#")]
            text = "\n".join(lines).strip()
            if not text:  # 纯说明文件（如 _guide.txt）没有正文，不该出现在下拉里
                continue
            templates.append({"name": path.stem, "text": text, "chars": len(text),
                              "is_script": "script" in path.stem})

    return {
        "auth_ok": auth_ok, "auth_mode": auth_mode, "auth_error": auth_error,
        "base_url": kc.base_url(), "voices": kc.VOICES, "templates": templates,
        "limits": {
            "prompt_max": kc.PROMPT_MAX_CHARS, "tts_max": kc.TTS_MAX_CHARS,
            "audio_max_mb": kc.AUDIO_MAX_MB, "image_max_mb": kc.IMAGE_MAX_MB,
            "audio_max_seconds": kc.AUDIO_MAX_SECONDS,
        },
    }


def save_upload(upload: UploadFile, job_id: str, kind: str, allowed: set[str], max_mb: int) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, f"{kind}仅支持 {'/'.join(sorted(allowed))}，收到 {suffix or '(无扩展名)'}")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / f"{job_id}_{kind}{suffix}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    size_mb = dest.stat().st_size / 1024 / 1024
    if size_mb > max_mb:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"{kind} {size_mb:.1f}MB 超过 {max_mb}MB 上限")
    return str(dest)


@app.post("/api/jobs")
async def create_job(
    image: UploadFile = File(..., description="角色图"),
    audio: UploadFile | None = File(None, description="音频；不传则用台词 TTS"),
    script: str = Form("", description="台词（不传音频时必填）"),
    prompt: str = Form(""),
    mode: str = Form("std"),
    voice: str = Form("genshin_vindi2"),
    voice_language: str = Form("zh"),
    voice_speed: float = Form(1.0),
    stabilize: str = Form(""),
    title: str = Form(""),
) -> JSONResponse:
    try:
        kc.resolve_auth()
    except kc.KlingError as exc:
        raise HTTPException(400, str(exc)) from None

    has_audio = audio is not None and bool(audio.filename)
    if not has_audio and not script.strip():
        raise HTTPException(400, "请上传音频，或填写台词以自动配音")
    if has_audio and script.strip():
        raise HTTPException(400, "音频和台词只能给一个：上传了音频就不会走 TTS")
    if mode not in ("std", "pro"):
        raise HTTPException(400, "mode 只能是 std 或 pro")
    if len(prompt) > kc.PROMPT_MAX_CHARS:
        raise HTTPException(400, f"提示词 {len(prompt)} 字符，超过 {kc.PROMPT_MAX_CHARS} 上限")
    if len(script) > kc.TTS_MAX_CHARS:
        raise HTTPException(400, f"台词 {len(script)} 字，超过 TTS 单次 {kc.TTS_MAX_CHARS} 字上限")
    if not 0.8 <= voice_speed <= 2.0:
        raise HTTPException(400, "语速需在 0.8~2.0 之间")

    job_id = uuid.uuid4().hex
    image_path = save_upload(image, job_id, "image", kc.IMAGE_EXTS, kc.IMAGE_MAX_MB)
    audio_path = save_upload(audio, job_id, "audio", kc.AUDIO_EXTS, kc.AUDIO_MAX_MB) if has_audio else None

    job = {
        "id": job_id,
        "title": title.strip() or f"任务 {datetime.now().strftime('%m-%d %H:%M')}",
        "stage": "queued", "percent": 0, "stage_label": "排队等待",
        "created_at": time.time(), "updated_at": time.time(),
        "image_path": image_path, "audio_path": audio_path,
        "script": script.strip(), "prompt": prompt.strip(), "mode": mode,
        "voice": voice, "voice_language": voice_language, "voice_speed": voice_speed,
        "stabilize": stabilize.strip(), "logs": [],
        "audio_source": "upload" if has_audio else "tts",
    }
    with _lock:
        _jobs[job_id] = job
    save_jobs()
    log(job_id, f"已创建（{'上传音频' if has_audio else '台词转配音'}，mode={mode}"
                f"{'，后处理=' + stabilize if stabilize else ''}）")
    _queue.put(job_id)
    return JSONResponse({"job_id": job_id})


def public_job(job: dict) -> dict:
    """去掉服务器本地路径等不必要暴露给前端的字段。"""
    hidden = {"image_path", "audio_path"}
    out = {k: v for k, v in job.items() if k not in hidden}
    elapsed = (job.get("finished_at") or time.time()) - job["created_at"]
    out["elapsed"] = int(elapsed)
    return out


@app.get("/api/jobs")
def list_jobs() -> dict:
    with _lock:
        jobs = [public_job(j) for j in _jobs.values()]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return {"jobs": jobs, "queue_size": _queue.qsize()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return public_job(job)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.pop(job_id, None)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.get("stage") not in ("done", "failed"):
        with _lock:
            _jobs[job_id] = job
        raise HTTPException(400, "任务还在进行中，无法删除")

    for key in ("image_path", "audio_path"):
        if job.get(key):
            Path(job[key]).unlink(missing_ok=True)
    for key in ("output_file", "raw_file"):
        if job.get(key):
            (OUTPUT_DIR / job[key]).unlink(missing_ok=True)
    save_jobs()
    return {"deleted": job_id}


@app.post("/api/tts-preview")
def tts_preview(text: str = Form(...), voice: str = Form("genshin_vindi2"),
                voice_language: str = Form("zh"), voice_speed: float = Form(1.0)) -> dict:
    """试听：合成一小段音频返回可播放 URL（同样会计费，只取前 60 字）。"""
    try:
        token_factory = kc.resolve_auth()[1]
        return kc.synthesize_speech(token_factory(), text.strip()[:60], voice,
                                    voice_language, voice_speed)
    except kc.KlingError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/download/{filename}")
def download_output(filename: str) -> FileResponse:
    path = (OUTPUT_DIR / filename).resolve()
    if not path.is_file() or OUTPUT_DIR.resolve() not in path.parents:
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=filename)


def main() -> None:
    parser = argparse.ArgumentParser(description="可灵数字人 Web 控制台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；需要内网访问填 0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    kc.load_dotenv()
    for directory in (UPLOAD_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if OUTPUT_DIR.is_dir():
        app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")

    load_jobs()
    threading.Thread(target=worker_loop, daemon=True).start()

    try:
        auth_mode, _ = kc.resolve_auth()
        print(f"鉴权就绪：{auth_mode} | 域名 {kc.base_url()}")
    except kc.KlingError as exc:
        print(f"警告：{exc}\n（服务照常启动，页面会提示，配好 .env 后刷新即可）")

    import uvicorn
    print(f"控制台地址： http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
