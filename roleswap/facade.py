"""高层门面：一行代码完成数字人长视频生成。

调用者只需面对语义清晰的 ``generate_digital_human(video, face, duration=180)``，
完全无需关心 ComfyUI 画布 / 节点、分段切割、重叠融合、音频合并等实现细节。
"""

from __future__ import annotations

from typing import Optional

from .client import RoleSwapClient
from .config import RoleSwapConfig
from .long_video import LongVideoProcessor, ProcessorParams
from .workflow_template import WorkflowOptions


def generate_digital_human(
    video: str,
    face: str,
    duration: int = 180,
    output_path: str = "output.mp4",
    *,
    seed: Optional[int] = None,
    steps: int = 6,
    cfg: float = 1.0,
    shift: float = 5.0,
    chunk_seconds: float = 3.5,
    overlap_frames: int = 12,
    max_parallel: int = 2,
    work_dir: Optional[str] = None,
    resume: bool = True,
    config: Optional[RoleSwapConfig] = None,
    workflow_options: Optional[WorkflowOptions] = None,
) -> str:
    """把表演视频中的人脸替换为目标人脸，生成指定时长的数字人长视频。

    Parameters
    ----------
    video:
        原始表演视频的本地路径。
    face:
        目标人脸照片（本地路径 / 公网 URL / base64）。
    duration:
        目标输出时长（秒），如 60 / 120 / 180。默认 180。
    output_path:
        最终 MP4 输出路径。默认 ``output.mp4``。
    seed:
        随机种子。默认 None，将自动生成并在所有片段间固定，保证人物一致。
    steps, cfg, shift:
        采样参数（有合理默认值，一般无需改动）。
    chunk_seconds:
        每段目标时长（秒），默认 3.5s（约 84 帧 @24fps）。
    overlap_frames:
        相邻段重叠帧数（默认 12），用于 crossfade 平滑过渡。
    max_parallel:
        有限并行提交数（默认 2）。
    work_dir:
        中间文件目录；保留即可支持断点续传。
    resume:
        是否启用断点续传（默认 True）。
    config:
        显式配置；None 时从环境变量 / .env 读取。

    Returns
    -------
    str
        最终视频文件路径。
    """
    client = RoleSwapClient(config=config)
    wf_opts = workflow_options or WorkflowOptions()
    wf_opts.steps = steps
    wf_opts.cfg = cfg
    wf_opts.shift = shift
    if seed is not None:
        wf_opts.seed = seed

    params = ProcessorParams(
        chunk_seconds=chunk_seconds,
        overlap_frames=overlap_frames,
        max_parallel=max_parallel,
        steps=steps,
        cfg=cfg,
        shift=shift,
        seed=seed,
        fps=wf_opts.fps,
        workflow_options=wf_opts,
    )
    processor = LongVideoProcessor(client=client, params=params)
    return processor.process(
        video=video,
        face_image=face,
        output_path=output_path,
        duration_seconds=duration,
        work_dir=work_dir,
        resume=resume,
    )
