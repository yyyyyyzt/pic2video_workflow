"""可选的 Wav2Lip 口型精修后处理。

SCAIL-2 端到端迁移已包含口型（属于面部动作的一部分），多数场景无需此步骤。
仅当对口型精度有更高要求时启用（``ProcessorParams.enable_wav2lip=True``）。

音画对齐说明：本库输出视频与源视频帧数、帧率严格 1:1，音频取自源视频原始
音轨且**从未被切割**，因此传给 Wav2Lip 的视频流与音频流天然严格对齐。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from ..config import load_settings
from ..errors import ScailSwapError


def run_wav2lip(video_path: str, audio_path: str, output_path: str) -> str:
    """对 ``video_path`` 按 ``audio_path`` 精修口型，写到 ``output_path``。

    需要在 .env 配置：
    - ``WAV2LIP_DIR``：Wav2Lip 仓库路径（setup.sh --with-wav2lip 可自动克隆）；
    - ``WAV2LIP_CHECKPOINT``：wav2lip_gan.pth 等权重路径；
    - ``WAV2LIP_PYTHON``：Wav2Lip 环境的 python（默认 python3）。
    """
    cfg = load_settings().wav2lip
    if not cfg.available:
        raise ScailSwapError(
            "未配置 Wav2Lip：请设置 WAV2LIP_DIR 与 WAV2LIP_CHECKPOINT，"
            "或运行 ./setup.sh --with-wav2lip"
        )
    inference = os.path.join(cfg.repo_dir, "inference.py")
    if not os.path.exists(inference):
        raise ScailSwapError(f"Wav2Lip inference.py 不存在：{inference}")

    with tempfile.TemporaryDirectory(prefix="wav2lip_") as tmp:
        tmp_out = os.path.join(tmp, "result.mp4")
        cmd = [
            cfg.python_bin, inference,
            "--checkpoint_path", cfg.checkpoint,
            "--face", os.path.abspath(video_path),
            "--audio", os.path.abspath(audio_path),
            "--outfile", tmp_out,
        ]
        proc = subprocess.run(
            cmd, cwd=cfg.repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            raise ScailSwapError(f"Wav2Lip 执行失败：\n{proc.stderr[-1500:]}")
        shutil.move(tmp_out, output_path)
    return output_path
