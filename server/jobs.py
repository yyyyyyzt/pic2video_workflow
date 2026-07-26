"""任务存储与后台执行器。

- 任务串行执行（单 GPU 的锚定链生成本身必须串行，多任务排队即可）；
- 状态落盘到 ``<job_dir>/job.json``，服务重启后历史任务仍可查询/下载；
- 进度由 LongVideoProcessor 的回调实时写入。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

from scailswap import LongVideoProcessor, ProcessorParams, create_engine
from scailswap.progress import ProgressEvent

logger = logging.getLogger("scailswap.server")


@dataclass
class Job:
    job_id: str
    job_dir: str
    source_image: str
    target_video: str
    engine: str
    params: dict = field(default_factory=dict)
    status: str = "queued"          # queued | running | done | failed
    percent: float = 0.0
    stage: str = ""
    message: str = "排队中"
    chunk_index: Optional[int] = None
    chunks_total: Optional[int] = None
    error: Optional[str] = None
    output_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("job_dir")
        d.pop("source_image")
        d.pop("target_video")
        return d


class JobStore:
    """内存 + 磁盘双写的任务库。"""

    def __init__(self, data_dir: str) -> None:
        self.jobs_dir = os.path.join(data_dir, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        for name in os.listdir(self.jobs_dir):
            meta = os.path.join(self.jobs_dir, name, "job.json")
            if not os.path.exists(meta):
                continue
            try:
                with open(meta, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                job = Job(**data)
                # 服务重启时，未完成的任务标记为失败（上传的临时状态无法恢复执行线程）
                if job.status in ("queued", "running"):
                    job.status = "failed"
                    job.error = "服务重启导致任务中断，请重新提交（work_dir 支持断点续传）"
                self._jobs[job.job_id] = job
            except (OSError, ValueError, TypeError):
                continue

    def new_job_dir(self) -> tuple[str, str]:
        job_id = uuid.uuid4().hex[:16]
        job_dir = os.path.join(self.jobs_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        return job_id, job_dir

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
        self.persist(job)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def persist(self, job: Job) -> None:
        job.updated_at = time.time()
        path = os.path.join(job.job_dir, "job.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(job), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


class JobRunner:
    """单 worker 后台执行器：从队列取任务 → 跑 LongVideoProcessor。"""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.running_job_id: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scailswap-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def submit(self, job: Job) -> None:
        self.store.add(job)
        self.queue.put(job.job_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            job = self.store.get(job_id)
            if job is None:
                continue
            self.running_job_id = job_id
            try:
                self._run(job)
            except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常都写回任务状态
                logger.exception("任务 %s 失败", job_id)
                job.status = "failed"
                job.error = f"{exc}\n{traceback.format_exc()[-1200:]}"
                job.message = "任务失败"
                self.store.persist(job)
            finally:
                self.running_job_id = None

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.message = "初始化引擎…"
        self.store.persist(job)

        params = ProcessorParams(**job.params)
        engine = create_engine(job.engine, output_dir=os.path.join(job.job_dir, "chunks"))
        processor = LongVideoProcessor(engine, params)

        last_persist = 0.0

        def on_progress(event: ProgressEvent) -> None:
            nonlocal last_persist
            job.percent = event.percent
            job.stage = event.stage
            job.message = event.message
            job.chunk_index = event.chunk_index
            job.chunks_total = event.chunks_total
            # 磁盘持久化限频（内存状态实时，磁盘 2s 一次）
            now = time.time()
            if now - last_persist > 2.0:
                self.store.persist(job)
                last_persist = now

        output_path = os.path.join(job.job_dir, "output.mp4")
        result = processor.process(
            source_image=job.source_image,
            driving_video=job.target_video,
            output_path=output_path,
            work_dir=os.path.join(job.job_dir, "work"),
            resume=True,
            on_progress=on_progress,
        )
        job.status = "done"
        job.percent = 100.0
        job.stage = "done"
        job.message = "生成完成"
        job.output_path = result
        self.store.persist(job)
