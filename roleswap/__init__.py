"""RoleSwap —— 角色替换（数字人）长视频生成封装库。

底层为一个基于 ComfyUI（WanVideo + MoCha）的换脸 / 角色替换推理服务，本库
将其封装为语义清晰、完全屏蔽画布 / 节点概念的 Python API：

基础用法（单段，≤5 秒）::

    from roleswap import RoleSwapClient

    client = RoleSwapClient()
    prompt_id = client.submit(video="perf.mp4", face_image="face.jpg")
    url = client.wait_for_result(prompt_id)

长视频用法（1~3 分钟）——一行搞定::

    from roleswap import generate_digital_human

    generate_digital_human("perf.mp4", "face.jpg", duration=180,
                           output_path="final.mp4")
"""

from .client import RoleSwapClient
from .config import RoleSwapConfig
from .facade import generate_digital_human
from .long_video import LongVideoProcessor
from .workflow_template import FRAME_LOAD_CAP, MODEL_CONTEXT_FRAMES, WorkflowOptions

__all__ = [
    "RoleSwapConfig",
    "RoleSwapClient",
    "LongVideoProcessor",
    "WorkflowOptions",
    "FRAME_LOAD_CAP",
    "MODEL_CONTEXT_FRAMES",
    "generate_digital_human",
]

__version__ = "0.1.0"
