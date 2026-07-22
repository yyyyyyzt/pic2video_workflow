"""持久化任务存储：任务状态写入磁盘，支持断线后查看与恢复。"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT_DIR / "web_jobs"


@dataclass
class Job:
    id: str
    status: str = "pending"  # pending | running | completed | failed | interrupted
    message: str = "等待开始"
    output_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 进度
    segments_done: int = 0
    segments_total: int = 0
    # 元信息
    video_name: str = ""
    face_name: str = ""
    duration: int = 0
    worker_pid: Optional[int] = None
    # 最近失败片段摘要
    failed_segments: List[int] = field(default_factory=list)
    segment_errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        known = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)  # type: ignore[arg-type]


class JobStore:
    """基于文件系统的任务仓库（``web_jobs/<job_id>/job.json``）。"""

    def __init__(self, jobs_dir: Optional[Path] = None) -> None:
        self.jobs_dir = Path(jobs_dir) if jobs_dir else JOBS_DIR
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    def log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "worker.log"

    def create(
        self,
        *,
        video_name: str,
        face_name: str,
        duration: int,
        manifest: Dict[str, Any],
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            video_name=video_name,
            face_name=face_name,
            duration=duration,
        )
        d = self.job_dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_manifest(job.id, manifest)
        self._save(job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        path = self.job_path(job_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return Job.from_dict(json.load(fh))
        except Exception:
            return None

    def get_manifest(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self.manifest_path(job_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def update(self, job_id: str, **fields: Any) -> None:
        job = self.get(job_id)
        if not job:
            return
        for key, val in fields.items():
            if hasattr(job, key):
                setattr(job, key, val)
        job.updated_at = time.time()
        self._save(job)

    def list_jobs(self, limit: int = 50) -> List[Job]:
        jobs: List[Job] = []
        if not self.jobs_dir.exists():
            return jobs
        for entry in self.jobs_dir.iterdir():
            if not entry.is_dir():
                continue
            job = self.get(entry.name)
            if job:
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def append_log(self, job_id: str, line: str) -> None:
        path = self.log_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")

    def read_log_tail(self, job_id: str, lines: int = 80) -> str:
        path = self.log_path(job_id)
        if not path.exists():
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return "".join(content[-lines:])

    def _save(self, job: Job) -> None:
        path = self.job_path(job.id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _write_manifest(self, job_id: str, manifest: Dict[str, Any]) -> None:
        path = self.manifest_path(job_id)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def is_pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def recover_stale_jobs(
    store: JobStore,
    *,
    grace_seconds: float = 180.0,
) -> int:
    """将 worker 已退出且长时间无心跳的 running 任务改为 interrupted。"""
    recovered = 0
    now = time.time()
    for job in store.list_jobs(limit=200):
        if job.status != "running":
            continue
        if is_pid_alive(job.worker_pid):
            continue
        # 刚更新过状态的任务可能正在启动 worker 或长轮询中，暂不判定中断
        if now - job.updated_at < grace_seconds:
            continue
        store.update(
            job.id,
            status="interrupted",
            message="后台进程已中断，可点击「继续任务」断点续传",
            worker_pid=None,
        )
        recovered += 1
    return recovered
