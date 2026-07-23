"""配置：全部来自环境变量 / .env，不硬编码。

分三组：
- 引擎选择与 ComfyUI 连接；
- ComfyUI 模型文件名（与 setup.sh 下载的文件对应，可替换为 fp8 等变体）；
- 托管 API（fal.ai）与 Wav2Lip 后处理。
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


@dataclass
class ComfyUIConfig:
    """自托管 ComfyUI 的连接与模型文件配置。"""

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
    engine: str = field(default_factory=lambda: _env("SCAILSWAP_ENGINE", "comfyui"))
    data_dir: str = field(default_factory=lambda: _env("SCAILSWAP_DATA_DIR", "./data"))
    comfyui: ComfyUIConfig = field(default_factory=ComfyUIConfig)
    fal: FalConfig = field(default_factory=FalConfig)
    wav2lip: Wav2LipConfig = field(default_factory=Wav2LipConfig)


def load_settings() -> Settings:
    """每次调用重新读取环境变量（便于测试注入）。"""
    return Settings()
