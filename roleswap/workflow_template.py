"""工作流请求体模板（ComfyUI input_values 格式）。

底层 API 使用 ``input_values`` 字典，键为 ``节点ID:字段名``（如 ``42:steps``、
``151:value``）。本模块将语义化参数映射到这些节点字段，固定参数写死在
``DEFAULT_INPUT_VALUES`` 中，用户只需关心少量可调项（见 ``WorkflowOptions``）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 单次 API 调用的帧数硬上限（节点 125:value，工作流默认 121 帧 ≈ 5s @24fps）。
FRAME_LOAD_CAP = 121
# 调试「不切片」模式允许的最大帧数（约 25s @24fps，需与工作流实际能力一致）
DEBUG_FRAME_LOAD_CAP = 600

SLICE_MODE_NORMAL = "normal"
SLICE_MODE_SINGLE = "single"
SLICE_MODE_HALVES = "halves"
VALID_SLICE_MODES = {SLICE_MODE_NORMAL, SLICE_MODE_SINGLE, SLICE_MODE_HALVES}

# 模型上下文窗口（节点 43:context_frames，约 3.3s @24fps）。
MODEL_CONTEXT_FRAMES = 81

# 节点 151:value —— False=角色替换，True=动作迁移
MODE_ROLE_SWAP = False
MODE_MOTION_TRANSFER = True

DEFAULT_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走"
)

# ---------------------------------------------------------------------------
# 工作流默认 input_values（来自已部署工作流，一般无需改动）
# ---------------------------------------------------------------------------
DEFAULT_INPUT_VALUES: Dict[str, Any] = {
    "42:force_offload": True,
    "42:batched_cfg": False,
    "42:add_noise_to_samples": False,
    "42:seed": 0,
    "42:steps": 6,
    "42:cfg": 1,
    "42:shift": 5,
    "42:scheduler": "dpm++_sde",
    "42:riflex_freq_index": 0,
    "42:denoise_strength": 1,
    "42:rope_function": "comfy",
    "42:start_step": 0,
    "42:end_step": -1,
    "42:guidance_mode": "cfg",
    "42:apg_eta": 0.5,
    "42:apg_momentum": 0,
    "42:apg_norm_threshold": 50,
    "42:apg_omega": 4,
    "42:apg_omega_I": 4.5,
    "42:apg_omega_TI": 4,
    "42:chain_omega_V": 1.25,
    "42:chain_omega_I": 4.5,
    "42:chain_omega_TI": 4,
    "43:freenoise": True,
    "43:verbose": False,
    "43:context_schedule": "uniform_standard",
    "43:context_frames": 81,
    "43:context_stride": 4,
    "43:context_overlap": 16,
    "43:fuse_method": "linear",
    "46:video": "",
    "46:custom_width": 0,
    "46:custom_height": 0,
    "46:skip_first_frames": 0,
    "46:select_every_nth": 1,
    "46:format": "AnimateDiff",
    "47:image": "",
    "48:aspect_ratio": "original",
    "48:proportional_width": 1,
    "48:proportional_height": 1,
    "48:fit": "crop",
    "48:method": "lanczos",
    "48:round_to_multiple": "32",
    "48:scale_to_side": "longest",
    "48:background_color": "#000000",
    "49:aspect_ratio": "original",
    "49:proportional_width": 1,
    "49:proportional_height": 1,
    "49:fit": "crop",
    "49:method": "lanczos",
    "49:round_to_multiple": "32",
    "49:scale_to_side": "longest",
    "49:background_color": "#000000",
    "50:use_cpu_cache": False,
    "50:verbose": False,
    "50:precision": "bf16",
    "51:offload_img_emb": True,
    "51:offload_txt_emb": True,
    "51:use_non_blocking": True,
    "51:block_swap_debug": False,
    "51:blocks_to_swap": 40,
    "51:vace_blocks_to_swap": 0,
    "51:prefetch_blocks": 1,
    "52:base_precision": "bf16",
    "52:quantization": "fp8_e4m3fn",
    "52:load_device": "offload_device",
    "52:attention_mode": "sageattn",
    "52:rms_norm_function": "default",
    "55:low_mem_load": False,
    "55:merge_loras": False,
    "55:strength": 1,
    "56:use_disk_cache": False,
    "56:precision": "bf16",
    "56:positive_prompt": "",
    "56:negative_prompt": DEFAULT_NEGATIVE_PROMPT,
    "56:quantization": "disabled",
    "56:device": "gpu",
    "58:force_offload": True,
    "58:strength_1": 1,
    "58:strength_2": 1,
    "58:crop": "disabled",
    "58:combine_embeds": "average",
    "58:tiles": 0,
    "58:ratio": 0.5,
    "61:enable_vae_tiling": False,
    "61:tile_x": 272,
    "61:tile_y": 272,
    "61:tile_stride_x": 144,
    "61:tile_stride_y": 128,
    "61:normalization": "default",
    "62:save_metadata": False,
    "62:trim_to_audio": False,
    "62:pingpong": False,
    "62:save_output": True,
    "62:filename_prefix": "Scail2/AnimateDiff",
    "62:loop_count": 0,
    "62:format": "video/h264-mp4",
    "62:pix_fmt": "yuv420p",
    "62:crf": 19,
    "66:match_image_size": True,
    "66:direction": "down",
    "67:match_image_size": True,
    "67:direction": "right",
    "68:save_metadata": False,
    "68:trim_to_audio": False,
    "68:pingpong": False,
    "68:save_output": False,
    "68:filename_prefix": "Scail2/AnimateDiff",
    "68:loop_count": 0,
    "68:format": "video/h264-mp4",
    "68:pix_fmt": "yuv420p",
    "68:crf": 19,
    "69:start_index": 0,
    "69:num_frames": 1,
    "91:detection_threshold": 0.5,
    "91:max_objects": 5,
    "91:detect_interval": 1,
    "95:text": "person,object",
    "96:object_indices": "",
    "96:sort_by": "left_to_right",
    "97:detection_threshold": 0.5,
    "97:max_objects": 5,
    "97:detect_interval": 1,
    "99:save_metadata": False,
    "99:trim_to_audio": False,
    "99:pingpong": False,
    "99:save_output": False,
    "99:filename_prefix": "AnimateDiff",
    "99:frame_rate": 24,
    "99:loop_count": 0,
    "99:format": "video/h264-mp4",
    "99:pix_fmt": "yuv420p",
    "99:crf": 19,
    "104:torchscript_jit": False,
    "104:refine_foreground": False,
    "104:rem_mode": "BEN2",
    "104:image_output": "Preview",
    "104:save_prefix": "ComfyUI",
    "104:add_background": "black",
    "123:value": 896,
    "124:value": 24,
    "125:value": FRAME_LOAD_CAP,
    "151:value": MODE_ROLE_SWAP,
    "157:offload_model": True,
    "157:offload_cache": True,
    "158:clean_file_cache": True,
    "158:clean_processes": True,
    "158:clean_dlls": True,
    "158:retry_times": 3,
    "159:force_offload": True,
    "159:tiled_vae": True,
    "159:prefix_alpha_crop": False,
    "159:preserve_main_ref_background": False,
    "159:single_frame_prefix_encoding": True,
    "159:by wuwukasi（bilibili）": True,
    "159:frame_window_size": 81,
    "159:pose_strength": 1,
    "159:ref_strength": 1,
    "159:transition_colormatch": "disabled",
    "159:loop_colormatch_reference": "previous_matched_frame",
}


@dataclass
class WorkflowOptions:
    """用户可调的工作流参数（映射到 ComfyUI 节点字段）。"""

    # 模式：role_swap=角色替换（151:value=False），motion_transfer=动作迁移（True）
    mode: str = "role_swap"
    steps: int = 6
    cfg: float = 1.0
    shift: float = 5.0
    seed: Optional[int] = None
    # 单次加载帧数上限（节点 125:value）
    frame_load_cap: int = FRAME_LOAD_CAP
    # 输出宽度（节点 123:value，0 表示自动）
    output_width: int = 896
    # 帧率（节点 124:value / 99:frame_rate）
    fps: int = 24
    # 视频起始帧跳过（节点 46:skip_first_frames，长视频分段时由处理器设置）
    skip_first_frames: int = 0
    positive_prompt: str = ""
    negative_prompt: str = field(default_factory=lambda: DEFAULT_NEGATIVE_PROMPT)
    pose_strength: float = 1.0
    ref_strength: float = 1.0
    context_overlap: int = 16
    # 抠图 / 背景（节点 104 BEN2、参考图缩放 48/49、检测 91/97）
    refine_foreground: bool = False
    rem_add_background: str = "green"
    preserve_main_ref_background: bool = False
    prefix_alpha_crop: bool = False
    detection_threshold: float = 0.5
    ref_background_color: str = "#FFFFFF"
    # 调试：允许 frame_load_cap 超过 FRAME_LOAD_CAP（配合不切片/少切片）
    relax_frame_cap: bool = False
    # 允许透传额外节点覆盖（高级用户）
    extra_input_values: Dict[str, Any] = field(default_factory=dict)

    def resolved_mode_value(self) -> bool:
        if self.mode == "motion_transfer":
            return MODE_MOTION_TRANSFER
        if self.mode == "role_swap":
            return MODE_ROLE_SWAP
        raise ValueError(
            f"未知 mode={self.mode!r}，应为 'role_swap' 或 'motion_transfer'"
        )


def build_payload(
    *,
    workflow_id: str,
    video: str,
    image: str,
    seed: int,
    options: Optional[WorkflowOptions] = None,
    num_frames: Optional[int] = None,
) -> Dict[str, Any]:
    """组装提交到 ``/api/workflow/generate`` 的请求体。

    Parameters
    ----------
    workflow_id:
        工作流 ID。
    video / image:
        视频与人脸引用（URL / base64 / 上传后的服务器文件名）。
    seed:
        随机种子（节点 42:seed）。
    options:
        用户可调参数；为 None 时使用默认值。
    num_frames:
        本次推理帧数（写入节点 125:value）。为 None 时使用 options.frame_load_cap。
    """
    opts = options or WorkflowOptions()
    frame_cap = num_frames if num_frames is not None else opts.frame_load_cap
    cap_limit = FRAME_LOAD_CAP
    if opts.relax_frame_cap:
        cap_limit = max(DEBUG_FRAME_LOAD_CAP, int(opts.frame_load_cap))

    if frame_cap > cap_limit:
        raise ValueError(
            f"num_frames/frame_load_cap={frame_cap} 超过上限 {cap_limit}。"
            f"{'调试模式' if opts.relax_frame_cap else '正常模式'}下请调整 duration 或 frame_load_cap。"
        )

    values = copy.deepcopy(DEFAULT_INPUT_VALUES)
    values.update({
        "42:seed": int(seed),
        "42:steps": int(opts.steps),
        "42:cfg": float(opts.cfg),
        "42:shift": float(opts.shift),
        "46:video": video,
        "46:skip_first_frames": int(opts.skip_first_frames),
        "46:force_rate": float(opts.fps),
        "47:image": image,
        "43:context_overlap": int(opts.context_overlap),
        "56:positive_prompt": opts.positive_prompt,
        "56:negative_prompt": opts.negative_prompt,
        "99:frame_rate": int(opts.fps),
        "123:value": int(opts.output_width),
        "124:value": int(opts.fps),
        "125:value": int(frame_cap),
        "151:value": opts.resolved_mode_value(),
        "159:pose_strength": float(opts.pose_strength),
        "159:ref_strength": float(opts.ref_strength),
        "159:preserve_main_ref_background": bool(opts.preserve_main_ref_background),
        "159:prefix_alpha_crop": bool(opts.prefix_alpha_crop),
        "104:refine_foreground": bool(opts.refine_foreground),
        "104:add_background": str(opts.rem_add_background),
        "91:detection_threshold": float(opts.detection_threshold),
        "97:detection_threshold": float(opts.detection_threshold),
        "48:background_color": str(opts.ref_background_color),
        "49:background_color": str(opts.ref_background_color),
    })
    if opts.extra_input_values:
        values.update(opts.extra_input_values)

    return {
        "workflow_id": workflow_id,
        "input_values": values,
    }
