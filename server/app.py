"""FastAPI 应用。

一键启动：
    ./scripts/start_api.sh
等价命令：
    uvicorn server.app:app --host 0.0.0.0 --port 8000

接口：
    POST /api/v1/jobs                 提交任务（source_image + target_video + 参数）
    GET  /api/v1/jobs                 任务列表
    GET  /api/v1/jobs/{job_id}        进度查询（percent 0~100）
    GET  /api/v1/jobs/{job_id}/download  下载生成视频
    GET  /health                      健康检查（含引擎连通性）
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import fields as dataclass_fields
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from scailswap import ProcessorParams, create_engine
from scailswap.config import load_settings
from scailswap.errors import ScailSwapError

from .jobs import Job, JobRunner, JobStore
from .schemas import HealthResponse, JobCreated, JobStatus

_settings = load_settings()
_store = JobStore(_settings.data_dir)
_runner = JobRunner(_store)

_ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_PARAM_FIELDS = {f.name for f in dataclass_fields(ProcessorParams)}


@asynccontextmanager
async def lifespan(_: FastAPI):
    _runner.start()
    yield
    _runner.stop()


app = FastAPI(
    title="ScailSwap API",
    description="SCAIL-2 长视频角色替换服务：照片 + 参考视频 → 替换后的长视频",
    version="1.0.0",
    lifespan=lifespan,
)


def _save_upload(upload: UploadFile, dest_dir: str, kind: str) -> str:
    ext = os.path.splitext(upload.filename or "")[1].lower()
    allowed = _ALLOWED_IMAGE_EXT if kind == "image" else _ALLOWED_VIDEO_EXT
    if ext not in allowed:
        raise HTTPException(400, f"{kind} 文件格式不支持：{ext}（可选 {sorted(allowed)}）")
    dest = os.path.join(dest_dir, f"{kind}{ext}")
    with open(dest, "wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    if os.path.getsize(dest) == 0:
        raise HTTPException(400, f"{kind} 文件为空")
    return dest


@app.post("/api/v1/jobs", response_model=JobCreated)
async def create_job(
    source_image: UploadFile = File(..., description="源角色照片（其人物将替换进视频）"),
    target_video: UploadFile = File(..., description="参考视频（提供动作/口型/场景）"),
    prompt: str = Form("", description="描述替换后的画面（建议详细描述角色外观与交互物体）"),
    mode: str = Form("replacement", description="replacement=角色替换 | animation=动作迁移"),
    engine: Optional[str] = Form(None, description="comfyui（默认，长视频）/ fal / fake"),
    seed: Optional[int] = Form(None),
    steps: Optional[int] = Form(None),
    enable_wav2lip: bool = Form(False, description="是否用 Wav2Lip 做口型精修后处理"),
    max_duration_seconds: Optional[float] = Form(None, description="只处理前 N 秒（调试用）"),
    params_json: str = Form(
        "{}",
        description="其余 ProcessorParams 字段的 JSON 覆盖，"
        '如 {"overlap_frames": 9, "resolution_tier": 704, "cfg": 1.0}',
    ),
):
    """提交生成任务。立即返回 job_id，生成在后台队列串行执行。"""
    if mode not in ("replacement", "animation"):
        raise HTTPException(400, f"mode 不合法：{mode}")
    try:
        overrides = json.loads(params_json or "{}")
        if not isinstance(overrides, dict):
            raise ValueError("params_json 必须是 JSON 对象")
    except ValueError as exc:
        raise HTTPException(400, f"params_json 解析失败：{exc}") from exc
    unknown = set(overrides) - _PARAM_FIELDS
    if unknown:
        raise HTTPException(400, f"params_json 含未知字段：{sorted(unknown)}")

    params: dict = {"prompt": prompt, "mode": mode, **overrides}
    if seed is not None:
        params["seed"] = seed
    if steps is not None:
        params["steps"] = steps
    if enable_wav2lip:
        params["enable_wav2lip"] = True
    if max_duration_seconds:
        params["max_duration_seconds"] = max_duration_seconds

    # 参数合法性提前校验（避免入队后才失败）
    try:
        ProcessorParams(**params)
    except TypeError as exc:
        raise HTTPException(400, f"参数不合法：{exc}") from exc

    job_id, job_dir = _store.new_job_dir()
    upload_dir = os.path.join(job_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    image_path = _save_upload(source_image, upload_dir, "image")
    video_path = _save_upload(target_video, upload_dir, "video")

    job = Job(
        job_id=job_id,
        job_dir=job_dir,
        source_image=image_path,
        target_video=video_path,
        engine=(engine or _settings.engine),
        params=params,
    )
    _runner.submit(job)
    return JobCreated(job_id=job_id, status=job.status)


def _to_status(job: Job) -> JobStatus:
    return JobStatus(
        job_id=job.job_id,
        status=job.status,
        percent=job.percent,
        stage=job.stage,
        message=job.message,
        chunk_index=job.chunk_index,
        chunks_total=job.chunks_total,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        download_url=f"/api/v1/jobs/{job.job_id}/download" if job.status == "done" else None,
    )


@app.get("/api/v1/jobs", response_model=list[JobStatus])
async def list_jobs():
    return [_to_status(j) for j in _store.all()]


@app.get("/api/v1/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str):
    job = _store.get(job_id)
    if job is None:
        raise HTTPException(404, f"任务不存在：{job_id}")
    return _to_status(job)


@app.get("/api/v1/jobs/{job_id}/download")
async def download_job(job_id: str):
    job = _store.get(job_id)
    if job is None:
        raise HTTPException(404, f"任务不存在：{job_id}")
    if job.status != "done" or not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(409, f"任务尚未完成（status={job.status}, percent={job.percent}）")
    return FileResponse(
        job.output_path,
        media_type="video/mp4",
        filename=f"scailswap_{job_id}.mp4",
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    try:
        engine_info = create_engine(_settings.engine).health_check()
    except ScailSwapError as exc:
        engine_info = {"engine": _settings.engine, "ok": False, "error": str(exc)}
    return HealthResponse(
        ok=bool(engine_info.get("ok")),
        engine=engine_info,
        queued_jobs=_runner.queue.qsize(),
        running_job=_runner.running_job_id,
    )
