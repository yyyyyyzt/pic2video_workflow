"""API 响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class JobCreated(BaseModel):
    job_id: str
    status: str
    detail: str = "任务已入队，可通过 GET /api/v1/jobs/{job_id} 轮询进度"


class JobStatus(BaseModel):
    job_id: str
    status: str                  # queued | running | done | failed
    percent: float = 0.0         # 全局进度百分比 0~100
    stage: str = ""              # prepare | generate | assemble | audio | postprocess | done
    message: str = ""
    chunk_index: Optional[int] = None
    chunks_total: Optional[int] = None
    error: Optional[str] = None
    created_at: float
    updated_at: float
    download_url: Optional[str] = None  # 完成后可下载


class HealthResponse(BaseModel):
    ok: bool
    engine: dict
    queued_jobs: int
    running_job: Optional[str] = None
