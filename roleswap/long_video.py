"""LongVideoProcessor —— 长视频处理器（核心难点）。

把 1~3 分钟的表演视频切成多个带重叠的短片段，逐段（可有限并行）提交到换脸
推理服务，再按重叠帧 crossfade 拼接，并合回原始音频，最终产出完整长视频。

内建：失败自动重试（最多 3 次）、断点续传（记录已完成片段，避免重复生成）。
"""

from __future__ import annotations

import json
import os
import random
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

from . import video_utils as vu
from . import workflow_template as wf
from .client import RoleSwapClient, RoleSwapError
from .log_utils import get_logger
from .workflow_template import WorkflowOptions

ProgressCallback = Callable[[str, dict], None]
logger = get_logger("roleswap.long_video")


@dataclass
class SegmentState:
    index: int
    start: int
    end: int
    status: str = "pending"  # pending | done | failed
    input_path: Optional[str] = None
    output_path: Optional[str] = None
    prompt_id: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None


@dataclass
class ProcessorParams:
    """长视频处理的可调参数。"""

    # 每段目标时长（秒），3~4 秒对应约 72~96 帧 @24fps，务必 < frame_load_cap
    chunk_seconds: float = 3.5
    # 相邻段重叠帧数（8~16），用于 crossfade 平滑过渡
    overlap_frames: int = 12
    # 有限并行提交数（1 表示串行）
    max_parallel: int = 2
    # 采样参数
    steps: int = 6
    cfg: float = 1.0
    shift: float = 5.0
    # 固定种子：所有片段共用同一 seed 以保证人物视觉一致（无跳跃）
    seed: Optional[int] = None
    # 输出帧率
    fps: int = 24
    # 单段失败最大重试次数
    max_retries: int = 3
    # ComfyUI 工作流可调参数（模式、提示词、强度等）
    workflow_options: Optional[WorkflowOptions] = None


class LongVideoProcessor:
    """长视频角色替换处理器。

    Parameters
    ----------
    client:
        基础客户端。为 None 时自动用环境变量创建。
    params:
        处理参数（见 ProcessorParams）。
    """

    def __init__(
        self,
        client: Optional[RoleSwapClient] = None,
        params: Optional[ProcessorParams] = None,
    ) -> None:
        self.client = client or RoleSwapClient()
        self.params = params or ProcessorParams()

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def process(
        self,
        video: str,
        face_image: str,
        output_path: str,
        duration_seconds: Optional[int] = None,
        work_dir: Optional[str] = None,
        resume: bool = True,
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        """处理整段长视频，返回最终 MP4 路径。

        Parameters
        ----------
        video:
            本地表演视频路径（长视频必须为本地文件，需切分并读取音频）。
        face_image:
            目标人脸（本地路径 / 公网 URL / base64）。会先解析一次并在各段复用。
        output_path:
            最终输出 MP4 路径。
        duration_seconds:
            目标输出时长（秒），如 60/120/180。None 表示使用整段视频。
        work_dir:
            中间文件目录（片段、状态文件等）。None 时基于 output_path 生成，
            保留该目录即可支持断点续传。
        resume:
            是否启用断点续传（默认 True）。
        """
        if not os.path.exists(video):
            raise RoleSwapError(
                f"长视频处理需要本地视频文件（用于切分与音频提取）：{video}"
            )

        params = self.params
        if params.seed is None:
            # 固定一个 seed 并写回，保证本次所有片段一致
            params.seed = random.randint(0, 2**32 - 1)

        info = vu.probe_video(video)
        fps = info.fps or float(params.fps)

        # 计算需要处理的总帧数（受 duration_seconds 限制）
        total_frames = info.frame_count
        if duration_seconds is not None:
            total_frames = min(total_frames, int(round(duration_seconds * fps)))
        if total_frames <= 0:
            raise RoleSwapError("待处理帧数为 0，请检查输入视频。")

        # 每段帧数：由 chunk_seconds 换算，并强制不超过工作流硬上限
        chunk_frames = int(round(params.chunk_seconds * fps))
        chunk_frames = min(chunk_frames, wf.FRAME_LOAD_CAP)
        if chunk_frames <= params.overlap_frames:
            raise RoleSwapError(
                f"chunk_frames({chunk_frames}) 必须大于 overlap_frames"
                f"({params.overlap_frames})，请调大 chunk_seconds。"
            )

        segments_plan = vu.plan_segments(
            total_frames=total_frames,
            chunk_frames=chunk_frames,
            overlap=params.overlap_frames,
        )

        work_dir = work_dir or (os.path.abspath(output_path) + ".roleswap_work")
        os.makedirs(work_dir, exist_ok=True)
        state_path = os.path.join(work_dir, "state.json")

        states = self._load_or_init_state(
            state_path, segments_plan, resume=resume, work_dir=work_dir
        )

        self._report_progress(
            on_progress,
            f"规划完成：共 {len(states)} 段",
            states,
        )

        # 预解析人脸输入（本地文件只上传一次，各段复用同一引用）
        self._report_progress(on_progress, "正在解析/上传人脸素材…", states)
        resolved_face = self.client._resolve_input(face_image, kind="image")

        print(
            f"[RoleSwap] 共 {len(states)} 段 | 每段 {chunk_frames} 帧 | "
            f"重叠 {params.overlap_frames} 帧 | seed={params.seed} | "
            f"并行 {params.max_parallel}"
        )

        # 1) 抽取所有尚未完成片段的输入短视频
        to_cut = [
            st
            for st in states
            if not (st.status == "done" and st.output_path and os.path.exists(st.output_path))
        ]
        if to_cut:
            self._report_progress(on_progress, f"正在切分视频片段（共 {len(to_cut)} 段）…", states)
            for i, st in enumerate(to_cut, start=1):
                seg = vu.Segment(index=st.index, start=st.start, end=st.end)
                seg_input = os.path.join(work_dir, f"seg_{st.index:04d}_in.mp4")
                if not os.path.exists(seg_input):
                    self._report_progress(
                        on_progress,
                        f"切分片段 {i}/{len(to_cut)}（段 {st.index}，帧 {st.start}-{st.end}）",
                        states,
                    )
                    vu.extract_segment(video, seg, seg_input, fps=params.fps)
                st.input_path = seg_input
            self._report_progress(on_progress, "切分完成，开始 GPU 推理", states)
        else:
            for st in states:
                if st.status == "done" and st.output_path and os.path.exists(st.output_path):
                    st.input_path = os.path.join(work_dir, f"seg_{st.index:04d}_in.mp4")
        self._save_state(state_path, states)

        # 兼容：已完成段确保 input_path 存在
        for st in states:
            if not st.input_path:
                candidate = os.path.join(work_dir, f"seg_{st.index:04d}_in.mp4")
                if os.path.exists(candidate):
                    st.input_path = candidate

        # 2) 并行 / 串行处理各片段（含重试 + 断点续传）
        pending = [
            st
            for st in states
            if not (st.status == "done" and st.output_path and os.path.exists(st.output_path))
        ]

        if pending:
            self._run_segments(
                pending=pending,
                resolved_face=resolved_face,
                params=params,
                work_dir=work_dir,
                state_path=state_path,
                states=states,
                on_progress=on_progress,
            )

        # 校验全部完成
        failed = [st for st in states if st.status != "done"]
        if failed:
            report = self._format_failure_report(failed)
            logger.error("长视频处理失败：\n%s", report)
            raise RoleSwapError(report)

        # 3) crossfade 拼接所有片段输出
        ordered_outputs = [st.output_path for st in sorted(states, key=lambda s: s.index)]
        merged_silent = os.path.join(work_dir, "merged_silent.mp4")
        self._report_progress(on_progress, "正在 crossfade 拼接片段…", states)
        print("[RoleSwap] 正在按重叠帧 crossfade 拼接片段 ...")
        vu.crossfade_concat(
            segment_paths=ordered_outputs,  # type: ignore[arg-type]
            overlap=params.overlap_frames,
            output_path=merged_silent,
            fps=float(params.fps),
        )

        # 4) 提取原始音频并合并回最终视频
        self._report_progress(on_progress, "正在合并原始音频…", states)
        print("[RoleSwap] 正在提取并合并原始音频 ...")
        audio_path = os.path.join(work_dir, "audio.aac")
        extracted = vu.extract_audio(video, audio_path)
        vu.mux_audio(merged_silent, extracted, output_path)

        print(f"[RoleSwap] 完成：{output_path}")
        self._report_progress(on_progress, "全部完成", states)
        return output_path

    @staticmethod
    def _report_progress(
        on_progress: Optional[ProgressCallback],
        message: str,
        states: List[SegmentState],
        *,
        log_progress: bool = True,
        current_segment: Optional[int] = None,
        remote_status: Optional[str] = None,
        active_prompt_id: Optional[str] = None,
    ) -> None:
        if not on_progress:
            return
        done = sum(1 for s in states if s.status == "done")
        failed = [s.index for s in states if s.status == "failed"]
        segment_errors = [
            {"index": s.index, "error": s.error, "attempts": s.attempts}
            for s in states
            if s.status == "failed" and s.error
        ][:10]
        extra: dict = {
            "segments_done": done,
            "segments_total": len(states),
            "failed_segments": failed,
            "segment_errors": segment_errors,
            "log_progress": log_progress,
        }
        if current_segment is not None:
            extra["current_segment"] = current_segment
        if remote_status:
            extra["remote_status"] = remote_status
        if active_prompt_id:
            extra["active_prompt_id"] = active_prompt_id
        on_progress(message, extra)

    @staticmethod
    def _format_failure_report(failed: List[SegmentState]) -> str:
        """汇总失败片段的详细错误，便于排查 API 问题。"""
        indices = [s.index for s in failed]
        lines = [
            f"仍有 {len(failed)} 段未成功：{indices}。",
            "可修复后重跑（断点续传将跳过已完成片段）。",
            "",
            "=== 失败片段详情（前 5 段）===",
        ]
        for st in failed[:5]:
            lines.append(f"\n--- 段 {st.index} | 尝试 {st.attempts} 次 | prompt_id={st.prompt_id} ---")
            lines.append(st.error or "(无错误信息)")
        if len(failed) > 5:
            lines.append(f"\n... 另有 {len(failed) - 5} 段失败，详见 work_dir/state.json")
        return "\n".join(lines)

    @staticmethod
    def _is_queue_wait_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "等待超时" in str(exc) or '"pending":true' in msg or "pending': true" in msg

    @staticmethod
    def _is_workflow_output_error(exc: Exception) -> bool:
        """工作流本身未产出视频（非网络/超时），重试无意义。"""
        msg = str(exc)
        return any(
            token in msg
            for token in (
                "未生成视频",
                "ComfyUI_temp_",
                "仅产出临时预览",
                "视频合成节点",
            )
        )

    # ------------------------------------------------------------------ #
    # 片段处理（含重试）
    # ------------------------------------------------------------------ #
    def _download_with_retries(
        self,
        output_url: str,
        dest_path: str,
        *,
        max_attempts: int = 3,
    ) -> None:
        """下载输出文件；client 内部会尝试多种 view URL 变体。"""
        last_err: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                self.client.download(output_url, dest_path)
                return
            except RoleSwapError as exc:
                last_err = exc
                if attempt < max_attempts:
                    logger.warning(
                        "下载第 %d/%d 次失败，%ds 后重试：%s",
                        attempt,
                        max_attempts,
                        min(2**attempt, 10),
                        exc,
                    )
                    time.sleep(min(2**attempt, 10))
        if last_err:
            raise last_err

    def _process_one(
        self,
        st: SegmentState,
        resolved_face: str,
        params: ProcessorParams,
        work_dir: str,
        states: List[SegmentState],
        on_progress: Optional[ProgressCallback] = None,
    ) -> SegmentState:
        """处理单个片段：提交 -> 等待 -> 下载。带重试。"""
        seg_output = os.path.join(work_dir, f"seg_{st.index:04d}_out.mp4")
        last_err: Optional[Exception] = None
        seg_frames = st.end - st.start
        input_size = (
            os.path.getsize(st.input_path) if st.input_path and os.path.exists(st.input_path) else 0
        )

        logger.info(
            "开始处理段 %d | 帧 %d-%d (%d帧) | 输入 %.2fMB",
            st.index,
            st.start,
            st.end,
            seg_frames,
            input_size / 1048576,
        )

        seg_label = f"段 {st.index + 1}/{len(states)}"

        for attempt in range(1, params.max_retries + 1):
            st.attempts = attempt
            try:
                wf_opts = params.workflow_options or WorkflowOptions()
                wf_opts.steps = params.steps
                wf_opts.cfg = params.cfg
                wf_opts.shift = params.shift
                wf_opts.seed = params.seed
                wf_opts.fps = params.fps
                wf_opts.skip_first_frames = 0  # 已上传裁剪后的片段

                # 已有 prompt_id 且上次是排队超时：继续等待，不重复提交
                if st.prompt_id and attempt > 1:
                    self._report_progress(
                        on_progress,
                        f"{seg_label}：续等 GPU 任务 {st.prompt_id[:8]}…",
                        states,
                        current_segment=st.index,
                        active_prompt_id=st.prompt_id,
                    )
                    output_url = self._wait_segment_result(
                        st, states, on_progress, st.prompt_id
                    )
                else:
                    self._report_progress(
                        on_progress,
                        f"{seg_label}：编码素材并提交 GPU…",
                        states,
                        current_segment=st.index,
                    )
                    prompt_id = self.client.submit(
                        video=st.input_path,
                        face_image=resolved_face,
                        steps=params.steps,
                        cfg=params.cfg,
                        shift=params.shift,
                        seed=params.seed,
                        options=wf_opts,
                        num_frames=seg_frames,
                    )
                    st.prompt_id = prompt_id
                    output_url = self._wait_segment_result(
                        st, states, on_progress, prompt_id
                    )

                self._report_progress(
                    on_progress,
                    f"{seg_label}：下载结果…",
                    states,
                    current_segment=st.index,
                    active_prompt_id=st.prompt_id,
                )
                self._download_with_retries(output_url, seg_output)

                st.output_path = seg_output
                st.status = "done"
                st.error = None
                logger.info(
                    "段 %d 完成（第 %d 次尝试）prompt_id=%s",
                    st.index,
                    attempt,
                    st.prompt_id,
                )
                print(f"[RoleSwap] 段 {st.index} 完成（第 {attempt} 次尝试）")
                return st
            except RoleSwapError as exc:
                last_err = exc
                st.error = f"{exc}\n{traceback.format_exc()}"
                # 推理已完成但下载失败时，不重复提交 GPU 任务
                if st.prompt_id and "下载失败" in str(exc):
                    logger.error(
                        "段 %d 推理已完成但下载失败 prompt_id=%s: %s",
                        st.index,
                        st.prompt_id,
                        exc,
                    )
                    break
                # 工作流未产出视频（仅 temp 图），不要重复提交
                if self._is_workflow_output_error(exc):
                    logger.error(
                        "段 %d 工作流未产出视频 prompt_id=%s: %s",
                        st.index,
                        st.prompt_id,
                        exc,
                    )
                    break
                # 排队/轮询超时：保留 prompt_id，下次只继续等待
                if st.prompt_id and self._is_queue_wait_error(exc):
                    logger.warning(
                        "段 %d GPU 仍在处理 prompt_id=%s，将续等而非重复提交",
                        st.index,
                        st.prompt_id,
                    )
                else:
                    st.prompt_id = None
                logger.error(
                    "段 %d 第 %d/%d 次失败: %s",
                    st.index,
                    attempt,
                    params.max_retries,
                    exc,
                )
                print(
                    f"[RoleSwap] 段 {st.index} 第 {attempt}/{params.max_retries} "
                    f"次失败：{exc}"
                )
                if attempt < params.max_retries:
                    time.sleep(min(2**attempt, 30))
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                st.error = f"{exc}\n{traceback.format_exc()}"
                logger.error(
                    "段 %d 第 %d/%d 次失败: %s",
                    st.index,
                    attempt,
                    params.max_retries,
                    exc,
                )
                print(
                    f"[RoleSwap] 段 {st.index} 第 {attempt}/{params.max_retries} "
                    f"次失败：{exc}"
                )
                if attempt < params.max_retries:
                    time.sleep(min(2**attempt, 30))

        st.status = "failed"
        st.error = str(last_err) if last_err else "unknown"
        return st

    def _wait_segment_result(
        self,
        st: SegmentState,
        states: List[SegmentState],
        on_progress: Optional[ProgressCallback],
        prompt_id: str,
    ) -> str:
        seg_label = f"段 {st.index + 1}/{len(states)}"

        def on_poll(info: dict) -> None:
            self._report_progress(
                on_progress,
                f"{seg_label}：{info.get('detail', info.get('status', '等待中'))}",
                states,
                log_progress=False,
                current_segment=st.index,
                remote_status=str(info.get("status") or ""),
                active_prompt_id=prompt_id,
            )

        return self.client.wait_for_result(prompt_id, on_poll=on_poll)

    def _run_segments(
        self,
        pending: List[SegmentState],
        resolved_face: str,
        params: ProcessorParams,
        work_dir: str,
        state_path: str,
        states: List[SegmentState],
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        """按 max_parallel 有限并行地处理待办片段。"""
        max_parallel = max(1, int(params.max_parallel))

        if max_parallel == 1:
            for st in pending:
                self._process_one(
                    st, resolved_face, params, work_dir, states, on_progress
                )
                self._save_state(state_path, states)
                self._report_progress(
                    on_progress,
                    f"片段完成 {sum(1 for s in states if s.status == 'done')}/{len(states)}",
                    states,
                )
            return

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {
                pool.submit(
                    self._process_one,
                    st,
                    resolved_face,
                    params,
                    work_dir,
                    states,
                    on_progress,
                ): st
                for st in pending
            }
            for fut in as_completed(futures):
                fut.result()
                self._save_state(state_path, states)
                self._report_progress(
                    on_progress,
                    f"片段进度 {sum(1 for s in states if s.status == 'done')}/{len(states)}",
                    states,
                )

    # ------------------------------------------------------------------ #
    # 断点续传：状态持久化
    # ------------------------------------------------------------------ #
    def _load_or_init_state(
        self,
        state_path: str,
        segments_plan: List[vu.Segment],
        resume: bool,
        work_dir: str,
    ) -> List[SegmentState]:
        """加载已有状态（断点续传），或根据切分计划初始化新状态。"""
        planned = {
            seg.index: SegmentState(index=seg.index, start=seg.start, end=seg.end)
            for seg in segments_plan
        }

        if resume and os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                for item in saved.get("segments", []):
                    idx = item.get("index")
                    if idx in planned:
                        # 仅当切分区间一致时才复用已保存状态
                        if (
                            item.get("start") == planned[idx].start
                            and item.get("end") == planned[idx].end
                        ):
                            st = SegmentState(**{
                                k: item.get(k)
                                for k in SegmentState.__dataclass_fields__
                                if k in item
                            })
                            # 若标记为 done 但输出文件已丢失，则回退为 pending
                            if st.status == "done" and not (
                                st.output_path and os.path.exists(st.output_path)
                            ):
                                st.status = "pending"
                                st.output_path = None
                            planned[idx] = st
                print(
                    "[RoleSwap] 断点续传：已加载历史状态，"
                    f"完成 {sum(1 for s in planned.values() if s.status == 'done')} 段"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[RoleSwap] 状态文件读取失败，将重新开始：{exc}")

        return [planned[i] for i in sorted(planned.keys())]

    @staticmethod
    def _save_state(state_path: str, states: List[SegmentState]) -> None:
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"segments": [asdict(s) for s in states]},
                fh,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, state_path)
