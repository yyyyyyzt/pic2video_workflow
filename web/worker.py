"""后台任务 Worker：独立进程执行长视频生成，与 Web 请求生命周期解耦。"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from dataclasses import fields

from roleswap import generate_digital_human
from roleswap.log_utils import setup_logging
from roleswap.workflow_template import WorkflowOptions
from web.job_store import JobStore, recover_stale_jobs


def _workflow_from_dict(data: dict) -> WorkflowOptions:
    valid = {f.name for f in fields(WorkflowOptions)}
    kwargs = {k: v for k, v in data.items() if k in valid}
    return WorkflowOptions(**kwargs)


def run_job(job_id: str) -> int:
    store = JobStore()
    job = store.get(job_id)
    manifest = store.get_manifest(job_id)
    if not job or not manifest:
        print(f"任务不存在：{job_id}", file=sys.stderr)
        return 1

    setup_logging(sink=lambda line: store.append_log(job_id, line))

    store.update(
        job_id,
        status="running",
        message="后台任务已启动，正在处理…",
        worker_pid=None,
        error=None,
    )
    store.append_log(job_id, f"[worker] 开始执行任务 {job_id}")
    store.append_log(
        job_id,
        f"[worker] 配置: input_mode={os.getenv('ROLESWAP_INPUT_MODE', 'base64')} "
        f"base_url={os.getenv('ROLESWAP_BASE_URL', '?')} "
        f"debug={os.getenv('ROLESWAP_DEBUG', '0')}",
    )

    wf_opts = _workflow_from_dict(manifest.get("workflow_options", {}))

    def on_progress(message: str, extra: dict | None = None) -> None:
        extra = extra or {}
        updates: dict = {"message": message}
        for key in ("segments_done", "segments_total", "failed_segments", "segment_errors"):
            if key in extra:
                updates[key] = extra[key]
        store.update(job_id, **updates)
        store.append_log(job_id, f"[progress] {message}")

    try:
        result = generate_digital_human(
            video=manifest["video_path"],
            face=manifest["face_path"],
            duration=manifest["duration"],
            output_path=manifest["output_path"],
            steps=wf_opts.steps,
            cfg=wf_opts.cfg,
            shift=wf_opts.shift,
            seed=wf_opts.seed,
            max_parallel=manifest.get("max_parallel", 1),
            work_dir=manifest.get("work_dir"),
            resume=manifest.get("resume", True),
            workflow_options=wf_opts,
            on_progress=on_progress,
        )
        store.update(
            job_id,
            status="completed",
            message="生成完成，可下载结果",
            output_path=result,
            worker_pid=None,
        )
        store.append_log(job_id, f"[worker] 完成：{result}")
        return 0
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        store.update(
            job_id,
            status="failed",
            message="生成失败",
            error=f"{exc}\n{tb}",
            worker_pid=None,
        )
        store.append_log(job_id, f"[worker] 失败：{exc}\n{tb}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RoleSwap 后台任务 Worker")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="执行指定任务")
    run_p.add_argument("job_id")

    sub.add_parser("recover", help="修复中断的 running 任务状态")

    args = parser.parse_args(argv)

    if args.command == "run":
        return run_job(args.job_id)

    if args.command == "recover":
        n = recover_stale_jobs(JobStore())
        print(f"已修复 {n} 个中断任务")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
