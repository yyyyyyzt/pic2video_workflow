"""最小使用示例：照片 + 参考视频 → 替换后的长视频。

运行前：
  1) ./setup.sh --with-comfyui   安装依赖并下载模型（GPU 机器）
  2) python3 ComfyUI/main.py --port 8188   启动推理端
  3) cp .env.example .env   （默认 COMFYUI_URL=http://127.0.0.1:8188 即可）
  4) 准备 face.jpg（源角色照片）与 performance.mp4（1~2 分钟参考视频）
"""

from scailswap import swap_character

if __name__ == "__main__":
    output = swap_character(
        source_image="face.jpg",          # 源角色照片
        target_video="performance.mp4",   # 参考视频（动作/口型/场景）
        output_path="final.mp4",
        prompt="一位金发男士穿黑色西装在街头演奏小提琴，行人从他身边走过",
        on_progress=lambda e: print(f"[{e.percent:5.1f}%] {e.stage}: {e.message}"),
    )
    print(f"生成完成：{output}")
