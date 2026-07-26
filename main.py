"""最小使用示例：照片 + 口播视频 → 替换后的长视频。

运行前：
  1) ./setup.sh --with-comfyui   安装依赖并下载 Wan2.2-Animate 模型（GPU 机器）
  2) python3 ComfyUI/main.py --port 8188   启动推理端
  3) cp .env.example .env   （默认 SCAILSWAP_ENGINE=wan_animate）
  4) 准备 face.jpg（源角色照片）与 talk.mp4（口播视频，时长无上限）
"""

from scailswap import swap_character

if __name__ == "__main__":
    output = swap_character(
        source_image="face.jpg",   # 源角色照片
        target_video="talk.mp4",   # 口播视频（提供口型/表情/动作/场景）
        output_path="final.mp4",
        prompt="一位穿深色衬衫的男士坐在办公室里对着镜头讲话，背景是书架",
        target_fps=16,             # 生成帧率：Wan 原生 16fps，帧数减半省算力
        output_fps=30,             # 成片帧率：升回 30fps
        on_progress=lambda e: print(f"[{e.percent:5.1f}%] {e.stage}: {e.message}"),
    )
    print(f"生成完成：{output}")
