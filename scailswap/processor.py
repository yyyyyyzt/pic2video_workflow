"""LongVideoProcessor —— 长视频角色替换的编排核心。

「长视频时间一致性」是怎么解决的（三层机制，缺一不可）
====================================================
1. **分块采样（chunking.py）**：按所选引擎的**原生训练配置**切块——
   Wan2.2-Animate 是 77 帧窗口 / 5 帧重叠 / 72 步进，SCAIL-2 是 81 / 5 / 76。
   相邻块共享同一批源帧，保证驱动信号本身连续。
2. **模型级锚定（engines/*）**：生成第 i+1 块时，把第 i 块的生成结果作为
   ``continue_motion``（Wan2.2-Animate）或 ``previous_frames``（SCAIL-2）传入。
   节点将其末尾 5 帧 VAE 编码后**冻结**为新块 latent 的头部（noise_mask=0，
   不加噪不重采样），模型在"已知开头"的条件下续写——身份、服装、光影、动作
   速度的连续性由模型语义保证。这与"先各自生成、再 FFmpeg 拼时间轴"有本质
   区别：后者每块独立采样，角色细节必然漂移。
3. **数值残差抹平（blending.py）**：VAE 编解码往返仍有极轻微像素差与低频
   颜色漂移，所以每块先做 Reinhard-LAB 颜色匹配（对齐上一块末帧），拼接时
   再对重叠区做余弦/高斯渐变融合。

锚定模式对策略的影响
====================
引擎的 :class:`AnchorMode` 决定分块与融合策略，processor 自动适配：

- ``LATENT``：用引擎原生 overlap（5 帧）+ crossfade 融合。最强一致性。
- ``REFERENCE``（如 wan2.7-videoedit）：引擎只吃参考图、拿不到 latent 锚点，
  新块开头是重新生成的像素。此时**强制 overlap=0**——做 crossfade 只会把两批
  不同像素叠成鬼影。改为把上一块输出的末帧作为附加参考图传入，并加强颜色
  匹配。属实验性路径，块边界仍可能轻微跳变。
- ``NONE``：只允许整段单块提交，多块直接拒绝（不做无一致性保障的拼接）。

帧率与时长
==========
``target_fps`` 可自由指定：processor 会先把整段驱动视频重采样到该帧率（时长
不变），之后所有分块/生成/拼接都基于它，最后把**未经改动的原始音轨**合回来，
音画天然对齐。这带来两个实际收益：Wan 系模型原生就在 16fps 上训练，把 30fps
的口播素材降到 16fps 生成，帧数直接减半（算力/费用同比下降）且更贴合训练分布；
``output_fps`` 则控制最终成片帧率，可选运动补偿插帧回 30fps。

时长无上限：逐块串行 + 断点续传 + 中间文件即用即删，5 分钟以上素材同样可跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from . import video_io as vio
from .blending import blend_overlap, reinhard_color_match
from .chunking import ChunkPlanner, ChunkSpec, ceil_to_4n1
from .engines.base import AnchorMode, ChunkTask, Engine
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

    # —— 分块与锚定 ——
    # None 表示采用所选引擎的原生训练配置（Wan2.2-Animate=77/5，SCAIL-2=81/5）。
    # 一般不要手动指定，偏离训练值会明显降低衔接质量。
    window_frames: Optional[int] = None
    overlap_frames: Optional[int] = None

    # —— 帧率（可自由改写，不再强制跟随源视频）——
    # 送入模型并用于分块/拼接的帧率。None=跟随源视频。
    # 口播场景推荐 16.0：Wan 系原生训练帧率，帧数减半且更贴合训练分布。
    target_fps: Optional[float] = None
    # 最终成片帧率。None=等于 target_fps。高于 target_fps 时按 interpolate 决定是否插帧。
    output_fps: Optional[float] = None
    # 升帧率时是否用 ffmpeg minterpolate 做运动补偿插帧（更顺滑但很慢）
    interpolate_output: bool = False

    # —— 生成参数 ——
    prompt: str = ""                         # 描述"替换后"的画面（详细描述效果更好）
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    mode: str = "replacement"                # replacement=角色替换 | animation=动作迁移
    steps: int = 6                           # lightx2v 蒸馏 LoRA 下 4~8 步即可
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

    # —— 长时长的磁盘管理 ——
    # 块完成后立即删除它的驱动分块与原始引擎输出（长素材必开，否则几十 GB 中间文件）
    cleanup_intermediate: bool = True
    # 分块中间视频编码：lossless（qp=0，锚定最保真）| crf（省 ~70% 空间）
    chunk_codec: str = "lossless"

    # —— 输入裁剪与后处理 ——
    max_duration_seconds: Optional[float] = None  # 只处理前 N 秒（调试用）
    enable_wav2lip: bool = False             # 可选 Wav2Lip 口型精修

    # —— SAM3 / 检测器跟踪 ——
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
        生成引擎。分块窗口与锚定策略会自动跟随引擎能力。
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
            参考（驱动）视频，提供动作、口型与场景。时长无上限。
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
        if params.chunk_codec not in ("lossless", "crf"):
            raise InvalidInputError(f"chunk_codec 需为 lossless 或 crf，当前 {params.chunk_codec}")

        work_dir = work_dir or (os.path.abspath(output_path) + ".scailswap_work")
        os.makedirs(work_dir, exist_ok=True)

        reporter.report("prepare", 0.1, "探测源视频信息…")
        info = vio.probe_video(driving_video)
        if info.frame_count <= 0:
            raise InvalidInputError("参考视频帧数为 0")

        # ---------- 帧率归一：整段重采样到目标生成帧率（时长不变） ----------
        gen_fps = float(params.target_fps or info.fps)
        source_for_chunks = driving_video
        if params.target_fps and abs(gen_fps - info.fps) > 0.01:
            resampled = os.path.join(work_dir, f"driving_{gen_fps:g}fps.mp4")
            if not os.path.exists(resampled):
                reporter.report(
                    "prepare", 0.3,
                    f"重采样驱动视频 {info.fps:.2f}fps → {gen_fps:g}fps（时长不变）…",
                )
                vio.resample_fps(driving_video, resampled, gen_fps)
            source_for_chunks = resampled
            total_frames = vio.count_frames(resampled)
        else:
            total_frames = info.frame_count

        if params.max_duration_seconds:
            total_frames = min(total_frames, int(round(params.max_duration_seconds * gen_fps)))
        if total_frames <= 0:
            raise InvalidInputError("待处理帧数为 0，请检查 target_fps / max_duration_seconds")

        width, height = params.width, params.height
        if not (width and height):
            width, height = vio.pick_resolution(info.width, info.height, params.resolution_tier)

        if params.seed is None:
            params.seed = random.randint(0, 2**31 - 1)

        window, overlap = self._resolve_chunk_config(gen_fps)
        chunks = self._plan(total_frames, window, overlap)

        signature = self._plan_signature(total_frames, gen_fps, width, height, window, overlap)
        states = self._load_states(work_dir, chunks, signature, resume)

        anchor_note = {
            AnchorMode.LATENT: "模型级锚定（latent 冻结）",
            AnchorMode.REFERENCE: "参考级弱锚定（实验性，不重叠切分）",
            AnchorMode.NONE: "无锚定（单块）",
        }[self.engine.anchor_mode]
        reporter.report(
            "prepare", 1.0,
            f"规划完成：{total_frames} 帧 @ {gen_fps:g}fps（{total_frames / gen_fps:.1f}s），"
            f"共 {len(chunks)} 块（窗口 {window} / 重叠 {overlap}），"
            f"{width}x{height}，{anchor_note}，seed={params.seed}",
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
                driving_video=source_for_chunks,
                source_image=source_image,
                prev_output=prev_output,
                chunk_out=chunk_out,
                fps=gen_fps,
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

            # 每块完成后立即释放推理端显存缓存
            #（自托管引擎内部执行等价于 torch.cuda.empty_cache() 的 /free 调用）
            self.engine.free_memory(aggressive=False)

        # ---------- 融合拼接（流式，不整段驻留内存） ----------
        reporter.report("assemble", 0.0, "开始拼接（重叠区渐变融合）…")
        silent_path = os.path.join(work_dir, "assembled_silent.mp4")
        written = self._assemble(chunks, states, silent_path, gen_fps, total_frames, reporter)
        if written != total_frames:
            raise ScailSwapError(f"拼接帧数校验失败：期望 {total_frames}，实际 {written}")

        # ---------- 音轨回填（时长与源一致，音画天然对齐） ----------
        reporter.report("audio", 0.2, "提取并合并原始音轨…")
        audio_path = vio.extract_audio(driving_video, os.path.join(work_dir, "audio.aac"))
        vio.mux_audio(silent_path, audio_path, output_path)
        reporter.report("audio", 0.7, "音轨合并完成")

        # ---------- 成片帧率改写（可选） ----------
        out_fps = params.output_fps
        if out_fps and abs(out_fps - gen_fps) > 0.01:
            reporter.report(
                "audio", 0.9,
                f"改写成片帧率 {gen_fps:g} → {out_fps:g}fps"
                f"（{'运动补偿插帧' if params.interpolate_output else '复帧'}）…",
            )
            retimed = os.path.join(work_dir, "retimed.mp4")
            vio.retime_fps(output_path, retimed, out_fps, interpolate=params.interpolate_output)
            shutil.move(retimed, output_path)
        reporter.report("audio", 1.0, "帧率处理完成")

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
    # 分块配置与规划
    # ------------------------------------------------------------------ #
    def _resolve_chunk_config(self, fps: float) -> tuple[int, int]:
        """决定窗口与重叠帧数：默认跟随引擎原生训练配置，并按锚定模式修正。"""
        params = self.params
        window = params.window_frames or self.engine.native_window
        window = ceil_to_4n1(window)

        # 托管 API 有秒数上限，换算成帧数后可能比原生窗口更小
        limit = getattr(self.engine, "chunk_frames_limit", None)
        if callable(limit):
            api_cap = ceil_to_4n1(int(limit(fps)))
            while api_cap > int(limit(fps)):
                api_cap -= 4
            window = min(window, max(5, api_cap))

        if self.engine.anchor_mode is AnchorMode.REFERENCE:
            # 参考级锚定：新块开头是重新生成的像素，crossfade 会产生鬼影，
            # 因此不重叠切分，一致性完全交给参考图 + 颜色匹配。
            return window, 0
        overlap = params.overlap_frames
        if overlap is None:
            overlap = self.engine.native_overlap
        return window, int(overlap)

    def _plan(self, total_frames: int, window: int, overlap: int) -> List[ChunkSpec]:
        planner = ChunkPlanner(window, overlap)
        probe = planner.plan(total_frames)
        if self.engine.anchor_mode is AnchorMode.NONE and len(probe) > 1:
            # 无锚定引擎绝不做多块拼接（那就是"伪造拼接"，身份必然漂移）
            raise InvalidInputError(
                f"引擎 {self.engine.name} 无跨块锚定能力，无法生成需要 {len(probe)} 块的长视频。"
                f"当前单块上限约 {window} 帧（{window / max(total_frames, 1):.0%} 的素材）。"
                "请改用 wan_animate / scail2 自托管引擎，"
                "或用 dashscope + DASHSCOPE_MODEL=wan2.7-videoedit（参考级锚定，实验性），"
                "或用 max_duration_seconds 缩短素材。"
            )
        if self.engine.anchor_mode is AnchorMode.NONE:
            gen_len = ceil_to_4n1(total_frames)
            return [
                ChunkSpec(
                    index=0, src_start=0, src_end=total_frames, overlap=0,
                    gen_length=gen_len, pad_frames=gen_len - total_frames,
                )
            ]
        return probe

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
        lossless = params.chunk_codec == "lossless"

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
            vio.write_chunk_video(frames, drv_path, fps=fps, lossless=lossless)
            del frames

        # 2) 组装锚点：LATENT 传上一块视频；REFERENCE 传上一块末帧图片
        anchor_video = None
        anchor_images: List[str] = []
        if chunk.index > 0 and prev_output:
            if self.engine.anchor_mode is AnchorMode.LATENT:
                anchor_video = prev_output
            elif self.engine.anchor_mode is AnchorMode.REFERENCE:
                anchor_img = os.path.join(work_dir, f"anchor_{chunk.index:04d}.png")
                if not os.path.exists(anchor_img):
                    vio.save_frame(prev_output, -1, anchor_img)
                anchor_images = [anchor_img]

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
            anchor_video=anchor_video,
            anchor_frames=max(1, chunk.overlap or self.engine.native_overlap),
            anchor_images=anchor_images,
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

        # 3) 生成（带重试；OOM 时先 aggressive 清显存再退避重试）
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
                chunk_progress(
                    0.0,
                    f"显存溢出，卸载模型并于 {wait:.0f}s 后重试"
                    f"（{attempt}/{params.max_retries}）",
                )
                self.engine.free_memory(aggressive=True)
                time.sleep(wait)
            except EngineError as exc:
                last_err = exc
                wait = params.retry_backoff * (2 ** (attempt - 1))
                chunk_progress(
                    0.0,
                    f"生成失败：{exc}，{wait:.0f}s 后重试（{attempt}/{params.max_retries}）",
                )
                time.sleep(wait)
        if raw_output is None:
            raise ScailSwapError(
                f"{label} 重试 {params.max_retries} 次仍失败：{last_err}"
            ) from last_err

        # 4) 统一帧率与帧数 → 颜色校正 → 写为本块最终输出（同时是下一块的锚点）
        gen_frames = self._normalize_chunk_frames(raw_output, chunk, fps, label, work_dir)
        if params.color_match and prev_output is not None:
            # 以上一块（已校正）末帧为基准做 Reinhard-LAB 匹配，阻断颜色漂移累积。
            # 锚定链使用校正后的帧，保证"模型看到的开头"与"最终拼接的内容"一致。
            ref_tail = vio.read_frames(prev_output)[-1]
            gen_frames = reinhard_color_match(
                gen_frames, ref_tail, strength=params.color_match_strength
            )
        vio.write_chunk_video(gen_frames, chunk_out, fps=fps, lossless=lossless)
        del gen_frames

        state.status = "done"
        state.output = chunk_out

        # 5) 长素材的磁盘管理：驱动分块与引擎原始输出用完即删
        if params.cleanup_intermediate:
            for path in (drv_path, raw_output):
                if path and os.path.exists(path) and path != chunk_out:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def _normalize_chunk_frames(
        self, raw_output: str, chunk: ChunkSpec, fps: float, label: str, work_dir: str
    ) -> list:
        """把引擎输出规整为「fps 帧率、恰好 src_length 帧」。

        托管 API（百炼 animate 系列）会强制输出 15/25fps，与我们的生成帧率不同，
        必须先重采样再裁帧，否则拼接后时长错乱。
        """
        source = raw_output
        got_info = vio.probe_video(raw_output)
        if abs(got_info.fps - fps) > 0.05:
            fixed = os.path.join(work_dir, f"chunk_{chunk.index:04d}_refps.mp4")
            vio.resample_fps(raw_output, fixed, fps)
            source = fixed

        frames = vio.read_frames(source)[: chunk.src_length]
        if len(frames) < chunk.src_length:
            # 服务端偶尔少 1~2 帧（容器/编码边界），复制末帧补齐
            if chunk.src_length - len(frames) > 2 or not frames:
                raise ScailSwapError(
                    f"{label} 生成帧数不足：期望 {chunk.src_length}，实际 {len(frames)}"
                )
            while len(frames) < chunk.src_length:
                frames.append(frames[-1].copy())
        if source != raw_output and os.path.exists(source):
            try:
                os.remove(source)
            except OSError:
                pass
        return frames

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

        overlap=0（参考级锚定）时退化为顺序直连——此时两块内容并非同一批像素，
        融合只会造成鬼影，一致性依赖参考图锚定与颜色匹配。
        """
        params = self.params
        writer: Optional[vio.StreamingVideoWriter] = None
        held: List = []  # 前一块留待融合的尾帧
        written = 0
        last = len(chunks) - 1

        for i, (chunk, st) in enumerate(zip(chunks, states)):
            if st.output is None:
                raise ScailSwapError(f"块 {i} 输出缺失，无法拼接")
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
                f"拼接 {i + 1}/{len(chunks)}",
                chunk_index=i, chunks_total=len(chunks),
            )
            del frames

        if writer is not None:
            writer.close()
        return written

    # ------------------------------------------------------------------ #
    # 断点续传状态
    # ------------------------------------------------------------------ #
    def _plan_signature(
        self, total_frames: int, fps: float, width: int, height: int, window: int, overlap: int
    ) -> str:
        p = self.params
        payload = json.dumps(
            [
                total_frames, round(fps, 4), width, height, window, overlap,
                p.seed, p.steps, p.cfg, p.shift, p.mode, p.prompt, self.engine.name,
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
