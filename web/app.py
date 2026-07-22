"""RoleSwap Web 测试页面 —— 上传视频/人脸、简单调参、异步生成与下载。

Ubuntu 服务器推荐用 gunicorn 启动（见 README「Web 测试页面」章节）。
"""

from __future__ import annotations

import os
import threading
import traceback
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from roleswap import generate_digital_human
from roleswap.workflow_template import FRAME_LOAD_CAP
from web.forms import parse_workflow_options, validate_workflow_options
from web.job_store import JobStore

ROOT_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT_DIR / "web_uploads"
OUTPUT_DIR = ROOT_DIR / "web_outputs"

ALLOWED_VIDEO = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

job_store = JobStore()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/")
    def index():
        return render_template("index.html", frame_load_cap=FRAME_LOAD_CAP)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

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
            workflow_options = parse_workflow_options(request.form)
        except (TypeError, ValueError):
            return jsonify({"error": "参数格式错误，请检查数字字段"}), 400

        wf_err = validate_workflow_options(workflow_options)
        if wf_err:
            return jsonify({"error": wf_err}), 400

        if duration < 1 or duration > 600:
            return jsonify({"error": "duration 建议在 1~600 秒之间"}), 400
        if not (1 <= max_parallel <= 8):
            return jsonify({"error": "max_parallel 建议在 1~8 之间"}), 400

        job = job_store.create()
        job_dir = UPLOAD_DIR / job.id
        job_dir.mkdir(parents=True, exist_ok=True)

        video_name = secure_filename(video.filename or "video.mp4") or "video.mp4"
        face_name = secure_filename(face.filename or "face.jpg") or "face.jpg"
        video_path = job_dir / video_name
        face_path = job_dir / face_name
        video.save(str(video_path))
        face.save(str(face_path))

        output_path = OUTPUT_DIR / f"{job.id}.mp4"
        work_dir = job_dir / "work"

        job_store.update(job.id, message="任务已创建，等待处理")

        thread = threading.Thread(
            target=_run_job,
            kwargs={
                "job_id": job.id,
                "video_path": str(video_path),
                "face_path": str(face_path),
                "output_path": str(output_path),
                "work_dir": str(work_dir),
                "duration": duration,
                "max_parallel": max_parallel,
                "workflow_options": workflow_options,
            },
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job.id, "status": "pending"})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = job_store.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({
            "job_id": job.id,
            "status": job.status,
            "message": job.message,
            "error": job.error,
            "download_url": (
                url_for("download_result", job_id=job.id)
                if job.status == "completed" and job.output_path
                else None
            ),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        })

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


def _run_job(
    *,
    job_id: str,
    video_path: str,
    face_path: str,
    output_path: str,
    work_dir: str,
    duration: int,
    max_parallel: int,
    workflow_options,
) -> None:
    job_store.update(job_id, status="running", message="正在生成，请耐心等待…")
    try:
        result = generate_digital_human(
            video=video_path,
            face=face_path,
            duration=duration,
            output_path=output_path,
            steps=workflow_options.steps,
            cfg=workflow_options.cfg,
            shift=workflow_options.shift,
            seed=workflow_options.seed,
            max_parallel=max_parallel,
            work_dir=work_dir,
            resume=True,
            workflow_options=workflow_options,
        )
        job_store.update(
            job_id,
            status="completed",
            message="生成完成，可下载结果",
            output_path=result,
        )
    except Exception as exc:  # noqa: BLE001
        job_store.update(
            job_id,
            status="failed",
            message="生成失败",
            error=f"{exc}\n{traceback.format_exc()}",
        )


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("ROLESWAP_WEB_PORT", "7860"))
    host = os.getenv("ROLESWAP_WEB_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, threaded=True)
