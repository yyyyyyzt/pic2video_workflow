"""RoleSwap Web 测试页面 —— 上传视频/人脸、后台任务、持久化状态查询。"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from roleswap.workflow_template import FRAME_LOAD_CAP, DEBUG_FRAME_LOAD_CAP
from web.forms import parse_workflow_options, validate_workflow_options
from web.job_store import JobStore, is_pid_alive, recover_stale_jobs

ROOT_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT_DIR / "web_uploads"
OUTPUT_DIR = ROOT_DIR / "web_outputs"

ALLOWED_VIDEO = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

job_store = JobStore()


def _spawn_worker(job_id: str) -> int:
    """启动独立后台 worker 进程（与 Web 请求生命周期解耦）。"""
    log_path = job_store.log_path(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    cmd = [sys.executable, "-m", "web.worker", "run", job_id]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    job_store.update(job_id, worker_pid=proc.pid, status="pending", message="已提交后台任务")
    return proc.pid


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    recover_stale_jobs(job_store)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            frame_load_cap=FRAME_LOAD_CAP,
            debug_frame_load_cap=DEBUG_FRAME_LOAD_CAP,
        )

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/jobs")
    def list_jobs():
        recover_stale_jobs(job_store)
        jobs = job_store.list_jobs(limit=50)
        return jsonify({
            "jobs": [_job_to_json(j) for j in jobs],
        })

    @app.post("/api/jobs")
    def create_job():
        video = request.files.get("video")
        face = request.files.get("face")
        if not video or not face:
            return jsonify({"error": "请同时上传视频和人脸图片"}), 400

        video_ext = Path(video.filename or "").suffix.lower()
        face_ext = Path(face.filename or "").suffix.lower()
        if video_ext not in ALLOWED_VIDEO:
            return jsonify({"error": f"不支持的视频格式：{video_ext}"}), 400
        if face_ext not in ALLOWED_IMAGE:
            return jsonify({"error": f"不支持的图片格式：{face_ext}"}), 400

        try:
            duration = int(request.form.get("duration", 60))
            max_parallel = int(request.form.get("max_parallel", 2))
            slice_mode = str(request.form.get("slice_mode", "normal")).strip() or "normal"
            workflow_options = parse_workflow_options(request.form)
        except (TypeError, ValueError):
            return jsonify({"error": "参数格式错误，请检查数字字段"}), 400

        wf_err = validate_workflow_options(workflow_options, slice_mode=slice_mode)
        if wf_err:
            return jsonify({"error": wf_err}), 400

        if duration < 1 or duration > 600:
            return jsonify({"error": "duration 建议在 1~600 秒之间"}), 400
        if not (1 <= max_parallel <= 8):
            return jsonify({"error": "max_parallel 建议在 1~8 之间"}), 400

        video_name = secure_filename(video.filename or "video.mp4") or "video.mp4"
        face_name = secure_filename(face.filename or "face.jpg") or "face.jpg"

        job = job_store.create(
            video_name=video_name,
            face_name=face_name,
            duration=duration,
            manifest={},  # 占位，下面写入真实 manifest
        )
        job_dir = UPLOAD_DIR / job.id
        job_dir.mkdir(parents=True, exist_ok=True)

        video_path = job_dir / video_name
        face_path = job_dir / face_name
        video.save(str(video_path))
        face.save(str(face_path))

        output_path = OUTPUT_DIR / f"{job.id}.mp4"
        work_dir = job_dir / "work"

        manifest = {
            "video_path": str(video_path),
            "face_path": str(face_path),
            "output_path": str(output_path),
            "work_dir": str(work_dir),
            "duration": duration,
            "max_parallel": max_parallel,
            "slice_mode": slice_mode,
            "resume": True,
            "workflow_options": asdict(workflow_options),
        }
        job_store._write_manifest(job.id, manifest)

        pid = _spawn_worker(job.id)
        job_store.update(
            job.id,
            status="pending",
            message="任务已提交后台，可关闭页面稍后查看",
            worker_pid=pid,
        )

        return jsonify({"job_id": job.id, "status": "pending"})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        recover_stale_jobs(job_store)
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        data = _job_to_json(job)
        data["log_tail"] = job_store.read_log_tail(job_id, lines=150)
        return jsonify(data)

    @app.post("/api/jobs/<job_id>/resume")
    def resume_job(job_id: str):
        """继续失败/中断的任务（断点续传）。"""
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        if job.status in {"running", "pending"} and is_pid_alive(job.worker_pid):
            return jsonify({"error": "任务仍在运行中"}), 400
        if job.status == "completed":
            return jsonify({"error": "任务已完成"}), 400

        pid = _spawn_worker(job_id)
        job_store.update(
            job_id,
            status="pending",
            message="已重新提交后台任务（断点续传）",
            worker_pid=pid,
            error=None,
        )
        return jsonify({"job_id": job_id, "status": "pending", "worker_pid": pid})

    @app.get("/download/<job_id>")
    def download_result(job_id: str):
        job = job_store.get(job_id)
        if not job or job.status != "completed" or not job.output_path:
            return redirect(url_for("index"))
        if not os.path.exists(job.output_path):
            return jsonify({"error": "输出文件不存在"}), 404
        return send_file(
            job.output_path,
            as_attachment=True,
            download_name=f"roleswap_{job_id[:8]}.mp4",
            mimetype="video/mp4",
        )

    return app


def _job_to_json(job) -> dict:
    progress = 0.0
    if job.segments_total > 0:
        progress = round(job.segments_done / job.segments_total * 100, 1)
    return {
        "job_id": job.id,
        "status": job.status,
        "message": job.message,
        "error": job.error,
        "video_name": job.video_name,
        "face_name": job.face_name,
        "duration": job.duration,
        "segments_done": job.segments_done,
        "segments_total": job.segments_total,
        "progress_percent": progress,
        "failed_segments": job.failed_segments,
        "segment_errors": job.segment_errors,
        "current_segment": job.current_segment,
        "remote_status": job.remote_status,
        "active_prompt_id": job.active_prompt_id,
        "worker_pid": job.worker_pid,
        "download_url": (
            f"/download/{job.id}" if job.status == "completed" and job.output_path else None
        ),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("ROLESWAP_WEB_PORT", "7860"))
    host = os.getenv("ROLESWAP_WEB_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, threaded=True)
