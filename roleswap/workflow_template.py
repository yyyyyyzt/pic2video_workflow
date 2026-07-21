"""工作流请求体模板。

设计目标：调用者只感知语义清晰的少量参数（video / image / steps / cfg / shift /
seed / frame_load_cap），而把与模型精度、显存优化、输出格式相关的大量固定参数
写死在这里，用户无需感知，也不会误改。

注意：底层是 ComfyUI，但本模块对外完全屏蔽画布 / 节点概念——它只负责把
「语义参数」组装成推理服务 ``/api/workflow/generate`` 所需的请求体。若后端字段名
与此处不同，只需在本文件集中调整映射即可，不影响其它模块。
"""

from __future__ import annotations

import copy
from typing import Any, Dict

# ---------------------------------------------------------------------------
# 内部固定参数（封装后隐藏）——与模型精度 / 显存优化 / 输出格式相关。
# 这些值来自已部署工作流的默认配置，调用者无需、也不应该修改。
# ---------------------------------------------------------------------------
FIXED_PARAMS: Dict[str, Any] = {
    # 显存优化：交换到 CPU 的 transformer block 数量（14B 模型 ~24GB 显存必需）
    "blocks_to_swap": 40,
    # VAE 平铺解码分块尺寸，降低峰值显存
    "tile_x": 272,
    "tile_y": 272,
    "tile_stride_x": 144,
    "tile_stride_y": 128,
    # 计算精度
    "precision": "bf16",
    "quantization": "disabled",
    # 注意力后端
    "attention_mode": "sdpa",
    # 输出格式（API 只输出图像帧，音频由本地拼接阶段补回）
    "output_format": "video/h264-mp4",
    "fps": 24,
    "pix_fmt": "yuv420p",
    "crf": 19,
    # 采样调度器（固定，不对外暴露）
    "scheduler": "unipc",
    # 单次推理硬约束：模型上下文窗口固定 81 帧（约 3.3s），当前工作流 frame_load_cap
    # 被硬编码为 121 帧（约 5s），单次调用无法直接生成超过 5s 的视频。
    "context_frames": 81,
}

# 单次 API 调用的帧数硬上限（工作流内 frame_load_cap 硬编码值）。
FRAME_LOAD_CAP = 121

# 模型上下文窗口（单次推理帧数上限，约 3.3s @ 24fps）。
MODEL_CONTEXT_FRAMES = 81


def build_payload(
    *,
    workflow_id: str,
    video: str,
    image: str,
    steps: int = 6,
    cfg: float = 1.0,
    shift: float = 5.0,
    seed: int,
    frame_load_cap: int = FRAME_LOAD_CAP,
) -> Dict[str, Any]:
    """组装提交到 ``/api/workflow/generate`` 的请求体。

    Parameters
    ----------
    workflow_id:
        工作流 ID。
    video:
        原始表演视频：公网 URL、base64 data，或上传后返回的服务器文件名。
    image:
        目标人脸照片：公网 URL、base64 data，或上传后返回的服务器文件名。
    steps:
        采样步数（推荐 6~10），默认 6。
    cfg:
        提示词引导强度（推荐 1.0~1.2），默认 1.0。
    shift:
        时序偏移量（推荐 5~8），默认 5.0。
    seed:
        随机种子。长视频各片段建议使用同一固定值以保证人物一致。
    frame_load_cap:
        单次加载帧数上限，默认 121（工作流硬编码值）。超过会被后端截断，
        因此本库在切分阶段就会保证每段不超过该值。

    Returns
    -------
    dict
        可直接 ``json=`` 提交的请求体。
    """
    if frame_load_cap > FRAME_LOAD_CAP:
        raise ValueError(
            f"frame_load_cap={frame_load_cap} 超过工作流硬上限 {FRAME_LOAD_CAP}，"
            "单次调用无法生成超过约 5 秒的视频。请使用 LongVideoProcessor 分段处理。"
        )

    # 用户可见参数
    user_params: Dict[str, Any] = {
        "video": video,
        "image": image,
        "steps": int(steps),
        "cfg": float(cfg),
        "shift": float(shift),
        "seed": int(seed),
        "frame_load_cap": int(frame_load_cap),
    }

    # 固定参数 + 用户参数合并（固定参数在前，用户参数不覆盖固定项之外的键）
    params: Dict[str, Any] = copy.deepcopy(FIXED_PARAMS)
    params.update(user_params)

    return {
        "workflow_id": workflow_id,
        # 兼容常见后端字段命名：既提供 params，也提供 inputs 别名。
        "params": params,
        "inputs": params,
    }
