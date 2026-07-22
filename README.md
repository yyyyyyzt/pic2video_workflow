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

推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖与启动（项目已内置 `pyproject.toml` / `uv.lock`）：

```bash
# 安装 uv（若尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 同步依赖（自动创建 .venv）
uv sync

# 需要系统已安装 ffmpeg / ffprobe
#   Ubuntu/Debian: sudo apt-get install -y ffmpeg
```

传统 pip 方式（可选）：

```bash
pip install -r requirements.txt
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
uv run python main.py
```

### 3) Web 测试页面（Ubuntu 服务器推荐）

提供一个简洁的 Web 界面：上传**表演视频 + 目标人脸**，填写参数后提交。任务在**服务器后台独立进程**中执行，关闭页面或断网不影响生成；可随时在「任务列表」查看进度、日志，失败后可「续传」断点恢复。

#### 一次性安装（Ubuntu 22.04/24.04，uv）

```bash
# 1. 系统依赖
sudo apt-get update
sudo apt-get install -y curl ffmpeg

# 2. 安装 uv（若尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env   # 或重新打开终端

# 3. 进入项目目录并同步依赖
cd /path/to/pic2video_workflow
uv sync

# 4. 配置推理 API
cp .env.example .env
nano .env   # 填入 ROLESWAP_BASE_URL / ROLESWAP_WORKFLOW_ID
```

#### 启动方式 A：开发调试（本机快速验证）

```bash
uv run python -m web
# 或
uv run roleswap-web
# 浏览器访问 http://<服务器IP>:7860
```

#### 启动方式 B：生产稳定运行（gunicorn，推荐）

```bash
chmod +x scripts/start_web.sh
./scripts/start_web.sh
```

等价手动命令：

```bash
uv run gunicorn --bind 0.0.0.0:7860 --workers 1 --threads 4 --timeout 3600 web.app:app
```

> **注意**：长视频任务耗时很长，且任务状态保存在内存中，请将 `--workers` 保持为 **1**。
> 多 worker 会导致任务状态不一致。

#### 后台常驻（nohup）

```bash
nohup ./scripts/start_web.sh > roleswap_web.log 2>&1 &
echo $! > roleswap_web.pid
# 查看日志：tail -f roleswap_web.log
# 停止：kill $(cat roleswap_web.pid)
```

#### 可选：systemd 服务

创建 `/etc/systemd/system/roleswap-web.service`：

```ini
[Unit]
Description=RoleSwap Web Test UI
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/path/to/pic2video_workflow
EnvironmentFile=/path/to/pic2video_workflow/.env
ExecStart=/bin/bash -lc 'cd /path/to/pic2video_workflow && ./scripts/start_web.sh'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now roleswap-web
sudo systemctl status roleswap-web
```

#### 防火墙放行（若从外网访问）

```bash
sudo ufw allow 7860/tcp
```

健康检查：

```bash
curl http://127.0.0.1:7860/health
```

### 4) 基础客户端——单段（≤5 秒）

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

| 参数 | 节点字段 | 默认 | 说明 |
| :-- | :-- | :-- | :-- |
| `video` | `46:video` | — | 表演视频 |
| `face` / `image` | `47:image` | — | 目标人脸 |
| `mode` | `151:value` | `role_swap` | `role_swap`=角色替换(False)，`motion_transfer`=动作迁移(True) |
| `steps` | `42:steps` | 6 | 采样步数 |
| `cfg` | `42:cfg` | 1.0 | 引导强度 |
| `shift` | `42:shift` | 5.0 | 时序偏移 |
| `seed` | `42:seed` | 随机 | 随机种子 |
| `frame_load_cap` | `125:value` | 121 | 单段帧数上限 |
| `output_width` | `123:value` | 896 | 输出宽度 |
| `fps` | `124:value` | 24 | 帧率 |
| `positive_prompt` | `56:positive_prompt` | "" | 正向提示词 |
| `negative_prompt` | `56:negative_prompt` | 内置 | 负向提示词 |
| `pose_strength` | `159:pose_strength` | 1.0 | 姿态强度 |
| `ref_strength` | `159:ref_strength` | 1.0 | 参考强度 |
| `duration` | — | 180 | 长视频目标时长（秒，本地分段） |

> API 请求体格式为 ``{"workflow_id": "...", "input_values": {"42:steps": 6, ...}}``。
> 其余节点参数已写死在 ``roleswap/workflow_template.py`` 的 ``DEFAULT_INPUT_VALUES`` 中。
> Web 测试台已暴露上述可调字段，并支持在「高级工作流参数」中修改。

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
- **输入兼容**：公网 URL / base64 / 本地文件。本地文件默认编码为
  ``data:*;base64`` 写入 ``input_values``（``ROLESWAP_INPUT_MODE=base64``），
  避免部分代理上传端点返回 405；也可设为 ``upload`` 或 ``auto``。

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
web/
  app.py               # Flask Web 测试页面
  job_store.py         # 内存任务状态
  templates/index.html
  static/              # 样式与前端脚本
scripts/
  start_web.sh         # uv + gunicorn 启动 Web
  sync.sh              # uv sync 同步依赖
main.py                # 5 行使用示例
pyproject.toml         # 项目依赖（uv 源）
uv.lock                # uv 锁定文件
requirements.txt       # pip 兼容（可选）
.env.example
```
