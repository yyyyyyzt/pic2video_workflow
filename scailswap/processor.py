"""LongVideoProcessor —— 长视频角色替换的编排核心。

「长视频时间一致性」是怎么解决的（三层机制，缺一不可）
====================================================
1. **分块采样（chunking.py）**：按模型训练配置切块——81 帧窗口、5 帧重叠、
   76 帧步进。相邻块共享同一批源帧，保证驱动信号本身连续。
2. **模型级锚定（engines/comfyui_engine.py）**：生成第 i+1 块时，把第 i 块的
   生成结果作为 ``previous_frames`` 传入 WanSCAILToVideo。节点将其末尾 5 帧
   VAE 编码后**冻结**为新块 latent 的头部（noise_mask=0，不加噪不重采样），
   模型在"已知开头"的条件下续写——身份、服装、光影、动作速度的连续性由
   模型语义保证。这与「先各自生成、再 FFmpeg 拼时间轴」有本质区别：后者
   每块独立采样，角色细节必然漂移；前者每块都以上一块的真实输出为条件。
3. **数值残差抹平（blending.py）**：VAE 编解码往返仍有极轻微像素差与低频
   颜色漂移，所以每块先做 Reinhard-LAB 颜色匹配（对齐上一块末帧），拼接时
   再对 5 帧重叠区做余弦/高斯渐变融合。这是"模型保证语义、融合抹平残差"，
   不是用融合代替语义一致性。

其余工程能力：断点续传（state.json）、失败/OOM 自动重试（指数退避 + 清显存）、
逐块显存释放、全程进度回调、输出帧率与源严格一致、原始音轨回填。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from . import video_io as vio
from .blending import blend_overlap, reinhard_color_match
from .chunking import DEFAULT_OVERLAP, DEFAULT_WINDOW, ChunkPlanner, ChunkSpec, ceil_to_4n1
from .engines.base import ChunkTask, Engine
from .errors import EngineError, EngineOOMError, InvalidInputError, ScailSwapError
from .progress import ProgressCallback, ProgressReporter

# Wan 系模型通用负向提示词（官方推荐）
DEFAULT_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
    "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
    "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
    "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)


@dataclass
class ProcessorParams:
    """长视频处理参数（API 的自定义参数最终都落到这里）。"""

    # —— 分块与锚定（默认为 SCAIL-2 训练配置，一般不要动）——
    window_frames: int = DEFAULT_WINDOW      # 每块帧数（4n+1）
    overlap_frames: int = DEFAULT_OVERLAP    # 相邻块重叠帧数（4n+1）

    # —— 生成参数 ——
    prompt: str = ""                         # 描述"替换后"的画面（详细描述效果更好）
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    mode: str = "replacement"                # replacement=角色替换 | animation=动作迁移
    steps: int = 6                           # lightx2v 蒸馏 LoRA 下 6~8 步即可
    cfg: float = 1.0
    shift: float = 5.0
    seed: Optional[int] = None               # None 时随机一次后全块共用
    width: Optional[int] = None              # None 时按源宽高比自动选择
    height: Optional[int] = None
    resolution_tier: int = 512               # 512 或 704

    # —— 融合与校色 ——
    blend_curve: str = "cosine"              # cosine | gaussian
    color_match: bool = True                 # Reinhard-LAB 逐块颜色对齐
    color_match_strength: float = 1.0

    # —— 稳健性 ——
    max_retries: int = 3                     # 单块最大重试（含 OOM 重试）
    retry_backoff: float = 5.0               # 重试基础退避秒数（指数增长）

    # —— 输入裁剪与后处理 ——
    max_duration_seconds: Optional[float] = None  # 只处理前 N 秒（调试用）
    enable_wav2lip: bool = False             # 可选 Wav2Lip 口型精修

    # —— SAM3 跟踪 ——
    video_object: str = "person"             # 驱动视频中要替换/跟踪的目标
    image_object: str = "person"             # 参考图中的目标
    max_objects: int = 1

    extra: dict = field(default_factory=dict)


@dataclass
class _ChunkState:
    index: int
    status: str = "pending"  # pending | done
    output: Optional[str] = None
    attempts: int = 0


class LongVideoProcessor:
    """长视频角色替换处理器。

    Parameters
    ----------
    engine:
        生成引擎（comfyui / fal / fake）。长视频（>1 块）要求引擎
        ``supports_anchor=True``。
    params:
        处理参数。
    """

    def __init__(self, engine: Engine, params: Optional[ProcessorParams] = None) -> None:
        self.engine = engine
        self.params = params or ProcessorParams()

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def process(
        self,
        source_image: str,
        driving_video: str,
        output_path: str,
        work_dir: Optional[str] = None,
        resume: bool = True,
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        """执行完整长视频流程，返回最终 MP4 路径。

        Parameters
        ----------
        source_image:
            源角色照片（其人脸/身体将替换进视频）。
        driving_video:
            参考（驱动）视频，提供动作、口型与场景。
        output_path:
            最终输出 MP4。
        work_dir:
            中间文件目录，保留即可断点续传。默认 ``<output>.scailswap_work``。
        resume:
            是否从 state.json 断点续传。
        on_progress:
            进度回调，收到 :class:`ProgressEvent`（含全局百分比）。
        """
        params = self.params
        reporter = ProgressReporter(on_progress)

        if not os.path.exists(source_image):
            raise InvalidInputError(f"源角色照片不存在：{source_image}")
        if not os.path.exists(driving_video):
            raise InvalidInputError(f"参考视频不存在：{driving_video}")

        reporter.report("prepare", 0.1, "探测源视频信息…")
        info = vio.probe_video(driving_video)
        if info.frame_count <= 0:
            raise InvalidInputError("参考视频帧数为 0")
        fps = info.fps

        total_frames = info.frame_count
        if params.max_duration_seconds:
            total_frames = min(total_frames, int(round(params.max_duration_seconds * fps)))

        width, height = params.width, params.height
        if not (width and height):
            width, height = vio.pick_resolution(info.width, info.height, params.resolution_tier)

        if params.seed is None:
            params.seed = random.randint(0, 2**31 - 1)

        chunks = self._plan(total_frames)
        work_dir = work_dir or (os.path.abspath(output_path) + ".scailswap_work")
        os.makedirs(work_dir, exist_ok=True)

        signature = self._plan_signature(total_frames, fps, width, height)
        states = self._load_states(work_dir, chunks, signature, resume)

        reporter.report(
            "prepare", 1.0,
            f"规划完成：{total_frames} 帧 @ {fps:.2f}fps，共 {len(chunks)} 块"
            f"（窗口 {params.window_frames} / 重叠 {params.overlap_frames}），"
            f"分辨率 {width}x{height}，seed={params.seed}",
            chunks_total=len(chunks),
        )

        # ---------- 逐块生成（锚定链决定必须串行） ----------
        total_new = sum(c.new_frames for c in chunks)
        done_new = 0
        prev_output: Optional[str] = None
        for chunk, st in zip(chunks, states):
            chunk_out = os.path.join(work_dir, f"chunk_{chunk.index:04d}_final.mp4")
            if st.status == "done" and st.output and os.path.exists(st.output):
                prev_output = st.output
                done_new += chunk.new_frames
                reporter.report(
                    "generate", done_new / total_new,
                    f"块 {chunk.index + 1}/{len(chunks)} 已完成（断点续传跳过）",
                    chunk_index=chunk.index, chunks_total=len(chunks),
                )
                continue

            if chunk.index > 0 and prev_output is None:
                raise ScailSwapError("锚定链断裂：上一块输出缺失，请清空 work_dir 重跑")

            self._generate_one_chunk(
                chunk=chunk,
                state=st,
                driving_video=driving_video,
                source_image=source_image,
                prev_output=prev_output,
                chunk_out=chunk_out,
                fps=fps,
                width=width,
                height=height,
                work_dir=work_dir,
                reporter=reporter,
                chunks_total=len(chunks),
                done_new=done_new,
                total_new=total_new,
            )
            prev_output = st.output
            done_new += chunk.new_frames
            self._save_states(work_dir, states, signature)

            # Phase 4：每块完成后立即释放推理端显存缓存
            # （ComfyUI 引擎内部执行等价于 torch.cuda.empty_cache() 的 /free 调用）
            self.engine.free_memory(aggressive=False)

        # ---------- 融合拼接（流式，不整段驻留内存） ----------
        reporter.report("assemble", 0.0, "开始重叠区渐变融合拼接…")
        silent_path = os.path.join(work_dir, "assembled_silent.mp4")
        written = self._assemble(chunks, states, silent_path, fps, total_frames, reporter)
        if written != total_frames:
            raise ScailSwapError(f"拼接帧数校验失败：期望 {total_frames}，实际 {written}")

        # ---------- 音轨回填（输出帧率/帧数与源严格一致，音画天然对齐） ----------
        reporter.report("audio", 0.2, "提取并合并原始音轨…")
        audio_path = vio.extract_audio(driving_video, os.path.join(work_dir, "audio.aac"))
        vio.mux_audio(silent_path, audio_path, output_path)
        reporter.report("audio", 1.0, "音轨合并完成")

        # ---------- 可选：Wav2Lip 口型精修 ----------
        if params.enable_wav2lip:
            if audio_path is None:
                reporter.report("postprocess", 1.0, "源视频无音轨，跳过 Wav2Lip")
            else:
                reporter.report("postprocess", 0.1, "Wav2Lip 口型精修中…")
                from .postprocess.wav2lip import run_wav2lip

                run_wav2lip(output_path, audio_path, output_path)
                reporter.report("postprocess", 1.0, "Wav2Lip 完成")

        reporter.done(f"完成：{output_path}")
        return output_path

    # ------------------------------------------------------------------ #
    # 分块规划
    # ------------------------------------------------------------------ #
    def _plan(self, total_frames: int) -> List[ChunkSpec]:
        params = self.params
        if not self.engine.supports_anchor:
            # 无锚定能力的引擎（如 fal）：整段一次提交，绝不做无锚定的分块拼接
            planner = ChunkPlanner(params.window_frames, params.overlap_frames)
            probe = planner.plan(total_frames)
            if len(probe) > 1:
                raise InvalidInputError(
                    f"引擎 {self.engine.name} 不支持 previous_frames 锚定，"
                    f"无法生成需要 {len(probe)} 块的长视频（硬约束：禁止无语义一致性的拼接）。"
                    "请改用 comfyui 引擎，或缩短视频。"
                )
            gen_len = ceil_to_4n1(total_frames)
            return [
                ChunkSpec(
                    index=0, src_start=0, src_end=total_frames, overlap=0,
                    gen_length=gen_len, pad_frames=gen_len - total_frames,
                )
            ]
        planner = ChunkPlanner(params.window_frames, params.overlap_frames)
        return planner.plan(total_frames)

    # ------------------------------------------------------------------ #
    # 单块生成（含 OOM / 失败重试）
    # ------------------------------------------------------------------ #
    def _generate_one_chunk(
        self,
        chunk: ChunkSpec,
        state: _ChunkState,
        driving_video: str,
        source_image: str,
        prev_output: Optional[str],
        chunk_out: str,
        fps: float,
        width: int,
        height: int,
        work_dir: str,
        reporter: ProgressReporter,
        chunks_total: int,
        done_new: int,
        total_new: int,
    ) -> None:
        params = self.params
        label = f"块 {chunk.index + 1}/{chunks_total}"

        # 1) 裁切本块驱动视频（帧精确），尾块复制末帧补齐到 4n+1
        drv_path = os.path.join(work_dir, f"chunk_{chunk.index:04d}_driving.mp4")
        if not os.path.exists(drv_path):
            frames = vio.read_frames(driving_video, chunk.src_start, chunk.src_length)
            if len(frames) < chunk.src_length:
                raise ScailSwapError(
                    f"{label} 源帧读取不足：期望 {chunk.src_length}，实际 {len(frames)}"
                )
            for _ in range(chunk.pad_frames):
                frames.append(frames[-1].copy())
            vio.write_chunk_video(frames, drv_path, fps=fps, lossless=True)
            del frames

        task = ChunkTask(
            index=chunk.index,
            driving_video=drv_path,
            reference_image=source_image,
            gen_length=chunk.gen_length,
            width=width,
            height=height,
            fps=fps,
            prompt=params.prompt,
            negative_prompt=params.negative_prompt,
            seed=int(params.seed or 0),
            steps=params.steps,
            cfg=params.cfg,
            shift=params.shift,
            mode=params.mode,
            anchor_video=prev_output if chunk.index > 0 else None,
            anchor_frames=params.overlap_frames,
            video_object=params.video_object,
            image_object=params.image_object,
            max_objects=params.max_objects,
            extra=dict(params.extra),
        )

        def chunk_progress(fraction: float, message: str) -> None:
            reporter.report(
                "generate",
                (done_new + fraction * chunk.new_frames) / total_new,
                f"{label}：{message}",
                chunk_index=chunk.index,
                chunks_total=chunks_total,
            )

        # 2) 生成（带重试；OOM 时先 aggressive 清显存再退避重试）
        last_err: Optional[Exception] = None
        raw_output: Optional[str] = None
        for attempt in range(1, params.max_retries + 1):
            state.attempts = attempt
            try:
                raw_output = self.engine.generate_chunk(task, on_progress=chunk_progress)
                break
            except EngineOOMError as exc:
                last_err = exc
                wait = params.retry_backoff * (2 ** (attempt - 1))
                chunk_progress(0.0, f"显存溢出，卸载模型并于 {wait:.0f}s 后重试（{attempt}/{params.max_retries}）")
                self.engine.free_memory(aggressive=True)
                time.sleep(wait)
            except EngineError as exc:
                last_err = exc
                wait = params.retry_backoff * (2 ** (attempt - 1))
                chunk_progress(0.0, f"生成失败：{exc}，{wait:.0f}s 后重试（{attempt}/{params.max_retries}）")
                time.sleep(wait)
        if raw_output is None:
            raise ScailSwapError(f"{label} 重试 {params.max_retries} 次仍失败：{last_err}") from last_err

        # 3) 裁掉尾部补帧 + 颜色校正 → 写为本块最终输出（同时是下一块的锚点）
        gen_frames = vio.read_frames(raw_output)
        gen_frames = gen_frames[: chunk.src_length]
        if len(gen_frames) < chunk.src_length:
            raise ScailSwapError(
                f"{label} 生成帧数不足：期望 ≥{chunk.src_length}，实际 {len(gen_frames)}"
            )
        if params.color_match and prev_output is not None:
            # 以上一块（已校正）末帧为基准做 Reinhard-LAB 匹配，阻断颜色漂移累积。
            # 注意锚定链使用校正后的帧，保证"模型看到的开头"与"最终拼接的内容"一致。
            ref_tail = vio.read_frames(prev_output)[-1]
            gen_frames = reinhard_color_match(
                gen_frames, ref_tail, strength=params.color_match_strength
            )
        vio.write_chunk_video(gen_frames, chunk_out, fps=fps, lossless=True)
        del gen_frames

        state.status = "done"
        state.output = chunk_out

    # ------------------------------------------------------------------ #
    # 融合拼接
    # ------------------------------------------------------------------ #
    def _assemble(
        self,
        chunks: List[ChunkSpec],
        states: List[_ChunkState],
        silent_path: str,
        fps: float,
        total_frames: int,
        reporter: ProgressReporter,
    ) -> int:
        """流式拼接所有块：重叠区做渐变融合，其余直写。返回写出的帧数。

        块 i（i>0）的前 overlap 帧与块 i-1 的末 overlap 帧对应**同一批源帧**，
        且经过模型锚定后内容几乎一致；此处用余弦/高斯权重逐像素过渡，
        把 VAE 往返的残余差异抹平（前块淡出、后块淡入）。
        """
        params = self.params
        writer: Optional[vio.StreamingVideoWriter] = None
        held: List = []  # 前一块留待融合的尾帧
        written = 0
        last = len(chunks) - 1

        for i, (chunk, st) in enumerate(zip(chunks, states)):
            assert st.output is not None
            frames = vio.read_frames(st.output)
            if writer is None:
                h, w = frames[0].shape[:2]
                writer = vio.StreamingVideoWriter(silent_path, fps, w, h)

            if i > 0 and held:
                ov = min(len(held), chunk.overlap, len(frames))
                blended = blend_overlap(held[:ov], frames[:ov], curve=params.blend_curve)
                for f in blended:
                    if written < total_frames:
                        writer.write(f)
                        written += 1
                frames = frames[ov:]

            next_overlap = chunks[i + 1].overlap if i < last else 0
            body = frames if next_overlap == 0 else frames[: len(frames) - next_overlap]
            for f in body:
                if written < total_frames:
                    writer.write(f)
                    written += 1
            held = [] if next_overlap == 0 else frames[len(frames) - next_overlap:]

            reporter.report(
                "assemble", (i + 1) / len(chunks),
                f"融合拼接 {i + 1}/{len(chunks)}",
                chunk_index=i, chunks_total=len(chunks),
            )
            del frames

        if writer is not None:
            writer.close()
        return written

    # ------------------------------------------------------------------ #
    # 断点续传状态
    # ------------------------------------------------------------------ #
    def _plan_signature(self, total_frames: int, fps: float, width: int, height: int) -> str:
        p = self.params
        payload = json.dumps(
            [
                total_frames, round(fps, 4), width, height,
                p.window_frames, p.overlap_frames, p.seed, p.steps, p.cfg, p.shift,
                p.mode, p.prompt, self.engine.name,
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @staticmethod
    def _state_path(work_dir: str) -> str:
        return os.path.join(work_dir, "state.json")

    def _load_states(
        self, work_dir: str, chunks: List[ChunkSpec], signature: str, resume: bool
    ) -> List[_ChunkState]:
        states = [_ChunkState(index=c.index) for c in chunks]
        path = self._state_path(work_dir)
        if not (resume and os.path.exists(path)):
            return states
        try:
            with open(path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved.get("signature") != signature:
                return states  # 参数变了，旧断点作废
            by_index = {item["index"]: item for item in saved.get("chunks", [])}
            for st in states:
                item = by_index.get(st.index)
                if item and item.get("status") == "done":
                    out = item.get("output")
                    if out and os.path.exists(out):
                        st.status = "done"
                        st.output = out
        except (OSError, ValueError, KeyError):
            pass
        return states

    def _save_states(self, work_dir: str, states: List[_ChunkState], signature: str) -> None:
        path = self._state_path(work_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"signature": signature, "chunks": [asdict(s) for s in states]},
                fh, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, path)
