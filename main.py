"""使用示例：5 行代码启动一个 3 分钟数字人长视频生成任务。

运行前：
  1) cp .env.example .env  并填入 ROLESWAP_BASE_URL / ROLESWAP_WORKFLOW_ID
  2) pip install -r requirements.txt  （并确保系统已安装 ffmpeg）
  3) 准备好本地表演视频 performance.mp4 与目标人脸 face.jpg
"""

from roleswap import generate_digital_human

if __name__ == "__main__":
    output = generate_digital_human(
        video="performance.mp4",   # 原始表演视频（本地路径）
        face="face.jpg",           # 目标人脸照片
        duration=180,              # 目标时长：180 秒
        output_path="final.mp4",   # 最终输出
    )
    print(f"生成完成：{output}")
