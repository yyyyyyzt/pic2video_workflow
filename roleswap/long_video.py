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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import List, Optional

from . import video_utils as vu
from . import workflow_template as wf
from .client import RoleSwapClient, RoleSwapError


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

        # 预解析人脸输入（本地文件只上传一次，各段复用同一引用）
        resolved_face = self.client._resolve_input(face_image, kind="image")

        print(
            f"[RoleSwap] 共 {len(states)} 段 | 每段 {chunk_frames} 帧 | "
            f"重叠 {params.overlap_frames} 帧 | seed={params.seed} | "
            f"并行 {params.max_parallel}"
        )

        # 1) 抽取所有尚未完成片段的输入短视频
        for st in states:
            if st.status == "done" and st.output_path and os.path.exists(st.output_path):
                continue
            seg = vu.Segment(index=st.index, start=st.start, end=st.end)
            seg_input = os.path.join(work_dir, f"seg_{st.index:04d}_in.mp4")
            if not os.path.exists(seg_input):
                vu.extract_segment(video, seg, seg_input, fps=params.fps)
            st.input_path = seg_input
        self._save_state(state_path, states)

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
            )

        # 校验全部完成
        failed = [st for st in states if st.status != "done"]
        if failed:
            raise RoleSwapError(
                f"仍有 {len(failed)} 段未成功：{[s.index for s in failed]}。"
                "可修复后重跑（断点续传将跳过已完成片段）。"
            )

        # 3) crossfade 拼接所有片段输出
        ordered_outputs = [st.output_path for st in sorted(states, key=lambda s: s.index)]
        merged_silent = os.path.join(work_dir, "merged_silent.mp4")
        print("[RoleSwap] 正在按重叠帧 crossfade 拼接片段 ...")
        vu.crossfade_concat(
            segment_paths=ordered_outputs,  # type: ignore[arg-type]
            overlap=params.overlap_frames,
            output_path=merged_silent,
            fps=float(params.fps),
        )

        # 4) 提取原始音频并合并回最终视频
        print("[RoleSwap] 正在提取并合并原始音频 ...")
        audio_path = os.path.join(work_dir, "audio.aac")
        extracted = vu.extract_audio(video, audio_path)
        vu.mux_audio(merged_silent, extracted, output_path)

        print(f"[RoleSwap] 完成：{output_path}")
        return output_path

    # ------------------------------------------------------------------ #
    # 片段处理（含重试）
    # ------------------------------------------------------------------ #
    def _process_one(
        self,
        st: SegmentState,
        resolved_face: str,
        params: ProcessorParams,
        work_dir: str,
    ) -> SegmentState:
        """处理单个片段：提交 -> 等待 -> 下载。带重试。"""
        seg_output = os.path.join(work_dir, f"seg_{st.index:04d}_out.mp4")
        last_err: Optional[Exception] = None

        for attempt in range(1, params.max_retries + 1):
            st.attempts = attempt
            try:
                prompt_id = self.client.submit(
                    video=st.input_path,  # 已是本地文件，submit 内部会自动上传
                    face_image=resolved_face,
                    steps=params.steps,
                    cfg=params.cfg,
                    shift=params.shift,
                    seed=params.seed,
                )
                st.prompt_id = prompt_id
                output_url = self.client.wait_for_result(prompt_id)
                self.client.download(output_url, seg_output)

                st.output_path = seg_output
                st.status = "done"
                st.error = None
                print(f"[RoleSwap] 段 {st.index} 完成（第 {attempt} 次尝试）")
                return st
            except Exception as exc:  # noqa: BLE001 - 需要捕获以便重试
                last_err = exc
                st.error = str(exc)
                print(
                    f"[RoleSwap] 段 {st.index} 第 {attempt}/{params.max_retries} "
                    f"次失败：{exc}"
                )
                # 指数退避
                if attempt < params.max_retries:
                    time.sleep(min(2**attempt, 30))

        st.status = "failed"
        st.error = str(last_err) if last_err else "unknown"
        return st

    def _run_segments(
        self,
        pending: List[SegmentState],
        resolved_face: str,
        params: ProcessorParams,
        work_dir: str,
        state_path: str,
        states: List[SegmentState],
    ) -> None:
        """按 max_parallel 有限并行地处理待办片段。"""
        max_parallel = max(1, int(params.max_parallel))

        if max_parallel == 1:
            for st in pending:
                self._process_one(st, resolved_face, params, work_dir)
                self._save_state(state_path, states)
            return

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {
                pool.submit(
                    self._process_one, st, resolved_face, params, work_dir
                ): st
                for st in pending
            }
            for fut in as_completed(futures):
                fut.result()  # 异常已在 _process_one 内吞掉并记录到 state
                self._save_state(state_path, states)

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
