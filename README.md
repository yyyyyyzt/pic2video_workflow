# RoleSwap · 角色替换（数字人）长视频生成封装库

把一个已部署的**换脸 / 角色替换推理服务**（底层 WanVideo + MoCha，运行于
ComfyUI）封装成语义清晰的 Python API。调用者**完全不需要理解画布 / 节点**，
只面对一个函数：

```python
from roleswap import generate_digital_human

generate_digital_human("performance.mp4", "face.jpg", duration=180,
                       output_path="final.mp4")
```

它会把表演视频里演员的脸替换成目标人脸，完美保留原始表情、动作与光影，并输出
一段 1~3 分钟、24fps、**带原始音频**的最终 MP4。

---

## 为什么需要“长视频处理器”

底层模型有两个硬约束：

- **模型上下文窗口固定 81 帧**（约 3.3 秒），受 14B 模型显存（~24GB）限制；
- **工作流内 `frame_load_cap` 被硬编码为 121 帧**（约 5 秒）。

因此**单次 API 调用无法直接生成超过 ~5 秒的视频**。本库通过如下工程方案突破：

```
分段切割 → 逐段(可并行)提交推理 → 按重叠帧 crossfade 融合 → FFmpeg 拼接 → 合回原始音频
```

---

## 安装

```bash
pip install -r requirements.txt
# 需要系统已安装 ffmpeg / ffprobe
#   Ubuntu/Debian: sudo apt-get install -y ffmpeg
#   macOS:         brew install ffmpeg
```

## 配置（不硬编码）

API 地址与工作流 ID 通过环境变量 / `.env` 读取：

```bash
cp .env.example .env
# 编辑 .env：
#   ROLESWAP_BASE_URL=https://your-comfy-host.com
#   ROLESWAP_WORKFLOW_ID=K11-SCAIL2动作迁移-角色替换-支持长视频-像素幻想Lab
#   ROLESWAP_API_KEY=（可选）
```

---

## 快速上手

### 1) 长视频（推荐）——一行搞定

```python
from roleswap import generate_digital_human

generate_digital_human(
    video="performance.mp4",
    face="face.jpg",
    duration=180,            # 目标时长（秒）
    output_path="final.mp4",
)
```

或直接运行示例：

```bash
python main.py
```

### 2) 基础客户端——单段（≤5 秒）

```python
from roleswap import RoleSwapClient

client = RoleSwapClient()                 # 自动读取 .env
prompt_id = client.submit(
    video="clip.mp4",                     # URL / base64 / 本地路径(自动上传)
    face_image="face.jpg",
    steps=6, cfg=1.0, shift=5.0, seed=42,
)
url = client.wait_for_result(prompt_id, timeout=600)
client.download(url, "clip_swapped.mp4")
```

---

## 暴露给用户的核心参数

| 参数 | 类型 | 默认 | 说明 |
| :-- | :-- | :-- | :-- |
| `video` | str | — | 表演视频（URL / base64 / 本地路径） |
| `face` / `image` | str | — | 目标人脸（URL / base64 / 本地路径） |
| `steps` | int | 6 | 采样步数（推荐 6~10） |
| `cfg` | float | 1.0 | 提示词引导强度（推荐 1.0~1.2） |
| `shift` | float | 5.0 | 时序偏移量（推荐 5~8） |
| `seed` | int | 随机 | 随机种子（长视频各段自动固定同值） |
| `duration` | int | 180 | 目标输出时长（秒） |

> 与模型精度 / 显存 / 输出格式相关的大量固定参数（`blocks_to_swap=40`、
> `tile_x=272`、`precision="bf16"`、`frame_load_cap=121` 等）已写死在
> `roleswap/workflow_template.py`，用户无需感知。

---

## 长视频处理器可调参数（`ProcessorParams`）

| 参数 | 默认 | 说明 |
| :-- | :-- | :-- |
| `chunk_seconds` | 3.5 | 每段目标时长（约 84 帧 @24fps，自动限制 < 121 帧） |
| `overlap_frames` | 12 | 相邻段重叠帧数（8~16），用于 crossfade |
| `max_parallel` | 2 | 有限并行提交数（1=串行） |
| `max_retries` | 3 | 单段失败最大重试次数 |
| `seed` | None | 固定后所有段共用，保证人物一致 |

---

## 关键设计说明

- **重叠帧 & crossfade**：相邻段共享 `overlap_frames` 帧源内容，拼接时在重叠区
  逐帧线性混合（前段淡出 / 后段淡入），消除接缝跳变。详见
  `roleswap/video_utils.py` 的 `plan_segments` 与 `crossfade_concat`。
- **种子一致性**：所有片段使用同一 `seed`，保证生成人物视觉一致、无跳跃。
- **音频处理**：API 只输出图像帧；本库单独提取原始音频，在最终拼接时合并回去。
- **断点续传**：处理状态写入 `work_dir/state.json`，中断后重跑将跳过已完成片段。
- **异常与重试**：单段失败自动重试（指数退避），最多 3 次。
- **输入兼容**：公网 URL / base64 / 本地文件（本地文件自动经
  `/api/comfy/upload/file` 上传）。

---

## 项目结构

```
roleswap/
  __init__.py          # 导出 RoleSwapClient / LongVideoProcessor / generate_digital_human
  config.py            # 从 .env 读取配置（不硬编码）
  workflow_template.py # 固定参数写死 + 用户参数注入
  client.py            # 基础客户端：submit / wait_for_result / upload / download
  video_utils.py       # 切分 / 重叠规划 / crossfade 拼接 / 音频提取合并
  long_video.py        # LongVideoProcessor：调度 / 并行 / 重试 / 断点续传
  facade.py            # generate_digital_human 高层门面
main.py                # 5 行使用示例
requirements.txt
.env.example
```
