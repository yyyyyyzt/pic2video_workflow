# ScailSwap · SCAIL-2 长视频角色替换

输入**一张真人照片**（源角色）+ **一段 1~2 分钟参考视频**（目标动作），输出
照片人物完美替换进视频的新视频——面部、身体动作、口型全部迁移，并保证
长视频的**时间一致性、身份稳定性与动作流畅度**。

技术路线的完整讨论与决策见 **[DESIGN.md](DESIGN.md)**（为什么选自托管
ComfyUI + 原生 `WanSCAILToVideo` 锚定，而不是官方推理脚本或 fal/WaveSpeed
托管 API）。

---

## 长视频一致性是怎么保证的（核心原理）

SCAIL-2 单次生成窗口只有 **81 帧**（约 3~5 秒）。本项目**不做**"独立分段 +
FFmpeg 时间轴拼接"（那会导致身份漂移与动作错位），而是复刻模型训练时的
长视频机制：

```
分块（Chunking）           81 帧窗口、5 帧重叠、76 帧步进（模型训练配置）
   │
重叠帧锚定（Anchoring）    生成第 i+1 块时，把第 i 块的生成结果作为
   │                      previous_frames 传入 WanSCAILToVideo：其末尾 5 帧
   │                      被 VAE 编码后「冻结」为新块 latent 的头部（不加噪、
   │                      不重采样），模型在已知开头的条件下续写
   │                      → 身份/服装/光影/动作速度由模型语义保证
颜色/身份校正（Matching）  每块生成后做 Reinhard-LAB 颜色统计对齐，
   │                      阻断逐块颜色漂移的累积
智能融合（Blending）       5 帧重叠区做余弦/高斯渐变权重逐像素融合，
   │                      抹平 VAE 编解码往返的残余数值差
输出                       帧率/帧数与源视频严格 1:1，原始音轨回填（音画对齐）
```

口型属于面部动作，SCAIL-2 端到端迁移时一并处理；如需进一步精修可开启
可选的 **Wav2Lip 后处理**（`enable_wav2lip=true`）。

---

## 项目结构

```
pic2video_workflow/
├── DESIGN.md                    # 技术路线讨论与决策（先读这个）
├── setup.sh                     # 一键安装（--with-comfyui 下载全套模型 / --with-wav2lip）
├── main.py                      # 5 行最小使用示例
├── scailswap/                   # ★ 核心库
│   ├── chunking.py              #   采样器：81/5/76 分块规划（4n+1 对齐）
│   ├── blending.py              #   余弦/高斯渐变融合 + Reinhard-LAB 颜色校正
│   ├── video_io.py              #   探测/帧精确裁切/流式写出/音轨提取合并
│   ├── processor.py             #   ★ LongVideoProcessor：调度/锚定链/重试/断点续传/进度
│   ├── progress.py              #   全局百分比进度回调
│   ├── facade.py                #   swap_character() 一行门面
│   ├── engines/
│   │   ├── base.py              #   引擎抽象（ChunkTask / supports_anchor）
│   │   ├── comfyui_engine.py    #   ★ 主引擎：原生节点构图 + previous_frames 锚定
│   │   │                        #     + /free 显存管理 + OOM 检测
│   │   ├── fal_engine.py        #   fal.ai 托管 API（仅 ≤81 帧短片验证）
│   │   └── fake_engine.py       #   无 GPU 调试引擎（CI 测试用）
│   └── postprocess/wav2lip.py   #   可选口型精修
├── server/                      # ★ FastAPI 服务
│   ├── app.py                   #   POST /api/v1/jobs 等接口
│   └── jobs.py                  #   后台任务队列 + 状态持久化
├── scripts/start_api.sh         # 一键启动 API
├── examples/
│   ├── test_api.py              # Python 调用测试脚本
│   └── test_api.sh              # curl 调用示例
├── tests/                       # 单元 + 集成测试（fake 引擎全链路，无 GPU 可跑）
├── roleswap/ + web/             # legacy：旧的远程工作流封装（已被新架构取代）
└── pyproject.toml / requirements.txt / .env.example
```

## 环境要求

- Python **3.10+**；ffmpeg / ffprobe
- 自托管推理端：CUDA **11.8+**，显存 ≥24GB（fp8 模型）/ ≥32GB（fp16）
- 也可将 ComfyUI 部署在另一台 GPU 机器上，通过 `COMFYUI_URL` 远程调用

## 安装与启动

```bash
# 1) 安装（编排层 + API 服务；GPU 机器上加 --with-comfyui 下载全套模型 ~45GB）
./setup.sh --with-comfyui

# 2) 配置
cp .env.example .env        # 默认 COMFYUI_URL=http://127.0.0.1:8188

# 3) 启动推理端（GPU 机器）
python3 ComfyUI/main.py --listen 127.0.0.1 --port 8188

# 4) 一键启动 API 服务
./scripts/start_api.sh      # 默认 0.0.0.0:8000，Swagger 文档在 /docs
```

## 使用

### Python 一行调用

```python
from scailswap import swap_character

swap_character(
    source_image="face.jpg",          # 源角色照片
    target_video="performance.mp4",   # 1~2 分钟参考视频
    output_path="final.mp4",
    prompt="一位金发男士穿黑色西装在街头演奏小提琴，行人从他身边走过",
    on_progress=lambda e: print(f"[{e.percent:5.1f}%] {e.message}"),
)
```

### HTTP API

```bash
# 提交任务
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -F "source_image=@face.jpg" \
  -F "target_video=@performance.mp4" \
  -F "prompt=一位金发男士穿黑色西装在街头演奏小提琴" \
  -F 'params_json={"seed": 42, "resolution_tier": 512}'
# → {"job_id": "a1b2c3...", "status": "queued"}

# 轮询进度（percent 0~100，含当前块序号与阶段）
curl http://127.0.0.1:8000/api/v1/jobs/<job_id>

# 完成后下载
curl -o final.mp4 http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

或直接用现成脚本：

```bash
python examples/test_api.py --image face.jpg --video performance.mp4 \
    --prompt "……" --output final.mp4
# / bash examples/test_api.sh face.jpg performance.mp4
```

## 核心参数

| 参数 | 默认 | 说明 |
| :-- | :-- | :-- |
| `prompt` | "" | 描述**替换后**的画面；写清角色外观与交互物体效果更好 |
| `mode` | `replacement` | `replacement`=角色替换（保留原场景）/ `animation`=动作迁移 |
| `window_frames` / `overlap_frames` | 81 / 5 | 分块窗口与锚定重叠（模型训练值，不建议改） |
| `steps` / `cfg` / `shift` | 6 / 1.0 / 5.0 | lightx2v 蒸馏 LoRA 下的采样配置 |
| `seed` | 随机 | 固定后全部分块共用 |
| `resolution_tier` | 512 | 512p / 704p（按源视频宽高比自动取 32 倍数） |
| `blend_curve` | `cosine` | 重叠区融合曲线：`cosine` / `gaussian` |
| `color_match` | `true` | Reinhard-LAB 逐块颜色对齐 |
| `enable_wav2lip` | `false` | Wav2Lip 口型精修后处理 |
| `max_duration_seconds` | 空 | 只处理前 N 秒（调试） |
| `video_object` / `image_object` | `person` | SAM3 开放词汇跟踪目标 |

## 稳健性设计

- **断点续传**：分块状态写入 `work_dir/state.json`，任务中断后重跑自动跳过
  已完成块（锚定链从最近完成块继续）。
- **OOM 自动重试**：检测到显存溢出时调用 ComfyUI `/free` 卸载模型 + 清缓存
 （等价 `torch.cuda.empty_cache()`），指数退避后重试（默认 3 次）。
- **逐块显存清理**：每块完成后立即 `/free` 释放缓存，长任务显存不累积。
- **进度回调**：`ProgressEvent{percent, stage, message, chunk_index}` 贯穿
  准备→逐块生成→融合→音频→后处理全流程，API 侧可实时轮询。
- **帧率保持**：输出帧数/帧率与源视频严格一致，原始音轨直接回填。

## 开发

```bash
uv sync
uv run pytest tests/ -q     # 全链路测试用 fake 引擎，无 GPU 可跑
uv run ruff check .
```
