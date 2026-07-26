"""配置：全部来自环境变量 / .env，不硬编码。

按引擎分组：
- :class:`WanAnimateConfig` —— 自托管 Wan2.2-Animate（**口播场景主引擎**）；
- :class:`DashScopeConfig` —— 阿里云百炼 API（wan2.2-animate-* / wan2.7-videoedit）；
- :class:`ComfyUIConfig` —— 自托管 SCAIL-2（备选：复杂 3D 动作 / 非人角色）；
- :class:`FalConfig` / :class:`Wav2LipConfig` —— fal.ai 短片与可选口型后处理。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv 为可选依赖
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    return float(raw) if raw else default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    return int(raw) if raw else default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class WanAnimateConfig:
    """自托管 Wan2.2-Animate（口播主引擎）的连接与模型配置。

    预处理链全部使用 ComfyUI 原生节点：RT-DETR 检测 + SDPose 关键点/人脸裁剪
    + SAM3 人物掩码，因此除下列权重外不需要任何第三方自定义节点。
    """

    base_url: str = field(default_factory=lambda: _env("COMFYUI_URL", "http://127.0.0.1:8188"))
    chunk_timeout: float = field(
        default_factory=lambda: _env_float("COMFYUI_CHUNK_TIMEOUT", 1800.0)
    )
    poll_interval: float = field(default_factory=lambda: _env_float("COMFYUI_POLL_INTERVAL", 2.0))
    http_timeout: float = field(default_factory=lambda: _env_float("COMFYUI_HTTP_TIMEOUT", 120.0))

    # Wan2.2-Animate 主模型（bf16 约 32GB；int8_convrot 显存紧张时用）
    unet: str = field(
        default_factory=lambda: _env("WANANIMATE_UNET", "wan2.2_animate_14B_bf16.safetensors")
    )
    unet_weight_dtype: str = field(
        default_factory=lambda: _env("WANANIMATE_UNET_DTYPE", "default")
    )
    text_encoder: str = field(
        default_factory=lambda: _env(
            "WANANIMATE_TEXT_ENCODER", "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
        )
    )
    vae: str = field(default_factory=lambda: _env("WANANIMATE_VAE", "wan_2.1_vae.safetensors"))
    clip_vision: str = field(
        default_factory=lambda: _env("WANANIMATE_CLIP_VISION", "clip_vision_h.safetensors")
    )
    # lightx2v 蒸馏 LoRA：4~8 步采样
    lora_lightx2v: str = field(
        default_factory=lambda: _env(
            "WANANIMATE_LORA_LIGHTX2V",
            "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
        )
    )
    lora_lightx2v_strength: float = field(
        default_factory=lambda: _env_float("WANANIMATE_LORA_LIGHTX2V_STRENGTH", 1.0)
    )
    # 官方 relight LoRA：仅替换模式启用，让人物与原场景光照色调一致
    lora_relight: str = field(
        default_factory=lambda: _env(
            "WANANIMATE_LORA_RELIGHT", "wan2.2_animate_14B_relight_lora_bf16.safetensors"
        )
    )
    lora_relight_strength: float = field(
        default_factory=lambda: _env_float("WANANIMATE_LORA_RELIGHT_STRENGTH", 1.0)
    )
    # 原生预处理模型
    sdpose_model: str = field(
        default_factory=lambda: _env("WANANIMATE_SDPOSE", "sdpose_wholebody_fp16.safetensors")
    )
    rtdetr_model: str = field(
        default_factory=lambda: _env("WANANIMATE_RTDETR", "rt_detr_v4-x-hgnet_fp16.safetensors")
    )
    sam3_checkpoint: str = field(
        default_factory=lambda: _env("WANANIMATE_SAM3_CKPT", "sam3.1_multiplex_fp16.safetensors")
    )
    pose_batch_size: int = field(default_factory=lambda: _env_int("WANANIMATE_POSE_BATCH", 16))
    face_crop_scale: float = field(
        default_factory=lambda: _env_float("WANANIMATE_FACE_CROP_SCALE", 1.5)
    )
    sampler_name: str = field(default_factory=lambda: _env("WANANIMATE_SAMPLER", "euler"))
    scheduler: str = field(default_factory=lambda: _env("WANANIMATE_SCHEDULER", "simple"))


@dataclass
class DashScopeConfig:
    """阿里云百炼（DashScope）API 配置。

    ``model`` 决定锚定能力：``wan2.7-videoedit`` 支持参考级伪锚定（可做长视频，
    实验性）；``wan2.2-animate-mix`` / ``-move`` 无锚定，仅支持单块 ≤30s。
    """

    api_key: str = field(default_factory=lambda: _env("DASHSCOPE_API_KEY"))
    base_url: str = field(
        default_factory=lambda: _env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com")
    )
    model: str = field(default_factory=lambda: _env("DASHSCOPE_MODEL", "wan2.2-animate-mix"))
    # animate 系列的服务模式：wan-std(15fps，便宜) / wan-pro(25fps，效果好)
    service_mode: str = field(default_factory=lambda: _env("DASHSCOPE_SERVICE_MODE", "wan-pro"))
    resolution: str = field(default_factory=lambda: _env("DASHSCOPE_RESOLUTION", "720P"))
    check_image: bool = field(default_factory=lambda: _env_bool("DASHSCOPE_CHECK_IMAGE", True))
    watermark: bool = field(default_factory=lambda: _env_bool("DASHSCOPE_WATERMARK", False))
    prompt_extend: bool = field(default_factory=lambda: _env_bool("DASHSCOPE_PROMPT_EXTEND", False))
    max_reference_images: int = field(
        default_factory=lambda: _env_int("DASHSCOPE_MAX_REF_IMAGES", 4)
    )
    default_edit_prompt: str = field(
        default_factory=lambda: _env(
            "DASHSCOPE_EDIT_PROMPT",
            "将视频中出镜讲话的人物替换为参考图中的人物，保持其原有的口型、表情、"
            "头部动作与身体姿态完全不变，背景、光照和镜头保持不变。",
        )
    )
    # 素材寻址：配置公网静态目录可避免 base64 体积限制
    public_base_url: str = field(default_factory=lambda: _env("DASHSCOPE_PUBLIC_BASE_URL"))
    public_root: str = field(default_factory=lambda: _env("DASHSCOPE_PUBLIC_ROOT"))
    max_base64_bytes: int = field(
        default_factory=lambda: _env_int("DASHSCOPE_MAX_BASE64_BYTES", 100 * 1024 * 1024)
    )
    submit_timeout: float = field(default_factory=lambda: _env_float("DASHSCOPE_SUBMIT_TIMEOUT", 600.0))
    task_timeout: float = field(default_factory=lambda: _env_float("DASHSCOPE_TASK_TIMEOUT", 1800.0))
    poll_interval: float = field(default_factory=lambda: _env_float("DASHSCOPE_POLL_INTERVAL", 5.0))
    download_timeout: float = field(
        default_factory=lambda: _env_float("DASHSCOPE_DOWNLOAD_TIMEOUT", 600.0)
    )


@dataclass
class ComfyUIConfig:
    """自托管 SCAIL-2 的连接与模型文件配置（备选引擎）。"""

    base_url: str = field(default_factory=lambda: _env("COMFYUI_URL", "http://127.0.0.1:8188"))
    # 单块推理的轮询超时（秒）。14B 模型 + 6 步蒸馏采样，单块一般 1~3 分钟
    chunk_timeout: float = field(default_factory=lambda: _env_float("COMFYUI_CHUNK_TIMEOUT", 1800.0))
    poll_interval: float = field(default_factory=lambda: _env_float("COMFYUI_POLL_INTERVAL", 2.0))
    http_timeout: float = field(default_factory=lambda: _env_float("COMFYUI_HTTP_TIMEOUT", 120.0))

    # 模型文件名（相对 ComfyUI models/ 各子目录），与 setup.sh 下载保持一致
    unet: str = field(
        default_factory=lambda: _env("SCAILSWAP_UNET", "wan2.1_14B_SCAIL_2_fp16.safetensors")
    )
    unet_weight_dtype: str = field(default_factory=lambda: _env("SCAILSWAP_UNET_DTYPE", "default"))
    text_encoder: str = field(
        default_factory=lambda: _env(
            "SCAILSWAP_TEXT_ENCODER", "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
        )
    )
    vae: str = field(default_factory=lambda: _env("SCAILSWAP_VAE", "wan_2.1_vae.safetensors"))
    clip_vision: str = field(
        default_factory=lambda: _env("SCAILSWAP_CLIP_VISION", "clip_vision_h.safetensors")
    )
    lora_lightx2v: str = field(
        default_factory=lambda: _env(
            "SCAILSWAP_LORA_LIGHTX2V",
            "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
        )
    )
    lora_lightx2v_strength: float = field(
        default_factory=lambda: _env_float("SCAILSWAP_LORA_LIGHTX2V_STRENGTH", 1.0)
    )
    lora_dpo: str = field(
        default_factory=lambda: _env("SCAILSWAP_LORA_DPO", "wan2.1_SCAIL_2_DPO_lora_bf16.safetensors")
    )
    lora_dpo_strength: float = field(
        default_factory=lambda: _env_float("SCAILSWAP_LORA_DPO_STRENGTH", 1.0)
    )
    sam3_checkpoint: str = field(
        default_factory=lambda: _env("SCAILSWAP_SAM3_CKPT", "sam3.1_multiplex_fp16.safetensors")
    )


@dataclass
class FalConfig:
    """fal.ai 托管 API 配置（仅短片引擎）。"""

    api_key: str = field(default_factory=lambda: _env("FAL_KEY"))
    model_id: str = field(default_factory=lambda: _env("FAL_SCAIL_MODEL", "fal-ai/scail-2"))
    timeout: float = field(default_factory=lambda: _env_float("FAL_TIMEOUT", 1800.0))
    poll_interval: float = field(default_factory=lambda: _env_float("FAL_POLL_INTERVAL", 3.0))


@dataclass
class Wav2LipConfig:
    """可选的 Wav2Lip 口型后处理。"""

    repo_dir: str = field(default_factory=lambda: _env("WAV2LIP_DIR"))
    checkpoint: str = field(default_factory=lambda: _env("WAV2LIP_CHECKPOINT"))
    python_bin: str = field(default_factory=lambda: _env("WAV2LIP_PYTHON", "python3"))

    @property
    def available(self) -> bool:
        return bool(self.repo_dir and self.checkpoint)


@dataclass
class Settings:
    # 默认引擎：wan_animate（自托管 Wan2.2-Animate，口播最优）
    engine: str = field(default_factory=lambda: _env("SCAILSWAP_ENGINE", "wan_animate"))
    data_dir: str = field(default_factory=lambda: _env("SCAILSWAP_DATA_DIR", "./data"))
    wan_animate: WanAnimateConfig = field(default_factory=WanAnimateConfig)
    dashscope: DashScopeConfig = field(default_factory=DashScopeConfig)
    comfyui: ComfyUIConfig = field(default_factory=ComfyUIConfig)
    fal: FalConfig = field(default_factory=FalConfig)
    wav2lip: Wav2LipConfig = field(default_factory=Wav2LipConfig)


def load_settings() -> Settings:
    """每次调用重新读取环境变量（便于测试注入）。"""
    return Settings()
