# ScailSwap · 长视频角色替换（口播优先）

输入**一张真人照片**（源角色）+ **一段任意时长的参考视频**（口播/表演），输出
照片人物完美替换进视频的新视频——面部表情、口型、身体动作全部迁移，并保证
长视频的**时间一致性、身份稳定性与动作流畅度**。

- **主引擎：自托管 Wan2.2-Animate** —— 面部表情与口型有独立的条件通路，口播场景最优
- **帧率可自由改写** —— 生成帧率与成片帧率解耦（降帧生成省一半算力，成片再升回 30fps）
- **时长无上限** —— 5 分钟以上素材照跑，逐块串行 + 断点续传 + 中间文件即用即删

选型依据与取舍详见 **[DESIGN.md](DESIGN.md)**（含 wan2.7 到底能不能用的完整结论）。

---

## 长视频一致性是怎么保证的

角色动画模型单次生成窗口只有几十帧（Wan2.2-Animate 77 帧 ≈ 4.8s @16fps）。
本项目**不做**"独立分段 + FFmpeg 时间轴拼接"（那会导致身份漂移），而是复刻模型
训练时的长视频机制：

```
分块（Chunking）          77 帧窗口、5 帧重叠、72 帧步进（模型原生训练配置）
   │
模型级锚定（Anchoring）    生成第 i+1 块时，把第 i 块的生成结果作为 continue_motion
   │                      传入 WanAnimateToVideo：其末尾 5 帧被 VAE 编码后「冻结」
   │                      为新块 latent 的头部（noise_mask=0，不加噪不重采样），
   │                      模型在已知开头的条件下续写
   │                      → 身份/服装/光影/动作速度由模型语义保证
颜色校正（Color Matching） 每块生成后做 Reinhard-LAB 颜色统计对齐，阻断逐块漂移累积
   │
智能融合（Blending）       5 帧重叠区做余弦/高斯渐变权重逐像素融合，
   │                      抹平 VAE 编解码往返的残余数值差
输出                       音轨取自源视频且全程未被切割，时长严格一致 → 音画天然对齐
```

口型属于面部动作，由 `face_video` 条件通路迁移；如需进一步精修可开启可选的
**Wav2Lip 后处理**（`enable_wav2lip=true`）。

### 三级锚定模式

引擎的锚定能力决定长视频的一致性上限，processor 会自动适配分块与融合策略：

| 模式 | 机制 | 分块策略 | 引擎 |
| :-- | :-- | :-- | :-- |
| `LATENT` | 上一块输出末 5 帧冻结为新块 latent 头部 | 重叠 5 帧 + crossfade | `wan_animate`、`scail2` |
| `REFERENCE` | 上一块末帧作为附加参考图（弱锚定，实验性） | **不重叠**（融合会出鬼影） | `dashscope` + `wan2.7-videoedit` |
| `NONE` | 无跨块锚定 | 只允许单块，多块直接拒绝 | `wan2.2-animate-mix`、`fal` |

## 引擎选择

| 引擎 | 部署 | 适用场景 |
| :-- | :-- | :-- |
| `wan_animate`（默认） | 自托管 GPU | **口播 / 说话视频最优**。面部表情、口型有专门条件通路 |
| `scail2` | 自托管 GPU | 复杂 3D 动作、多人交互、非人角色；低分辨率更稳 |
| `dashscope` | 阿里云百炼 API | 无 GPU 验证。`wan2.7-videoedit` 可跑长视频（实验性）；`wan2.2-animate-mix` 仅 ≤30s 单块 |
| `fal` | fal.ai API | ≤81 帧短片快速验证 |
| `fake` | 无 | CI 测试用调试引擎 |

## 项目结构

```
pic2video_workflow/
├── DESIGN.md                        # 选型讨论与决策（先读这个）
├── DEPLOY_AUTODL.md                 # AutoDL 部署：aria2 手动下模型 / 磁盘规划 / 启动
├── setup.sh                         # 一键安装（--with-comfyui / --with-scail2 / --with-wav2lip）
├── main.py                          # 最小使用示例
├── scailswap/                       # ★ 核心库
│   ├── chunking.py                  #   分块规划（窗口/重叠/4n+1 对齐，支持 overlap=0）
│   ├── blending.py                  #   余弦/高斯渐变融合 + Reinhard-LAB 颜色校正
│   ├── video_io.py                  #   探测/帧精确裁切/帧率重采样/流式写出/音轨合并
│   ├── processor.py                 #   ★ LongVideoProcessor：调度/锚定链/帧率/重试/续传
│   ├── progress.py                  #   全局百分比进度回调
│   ├── facade.py                    #   swap_character() 一行门面
│   ├── engines/
│   │   ├── base.py                  #   引擎抽象（AnchorMode / ChunkTask / 原生窗口）
│   │   ├── wan_animate_engine.py    #   ★ 主引擎：Wan2.2-Animate，全原生节点预处理链
│   │   ├── dashscope_engine.py      #   百炼 API：animate-mix/move + wan2.7-videoedit
│   │   ├── comfyui_engine.py        #   备选：SCAIL-2 自托管
│   │   ├── fal_engine.py            #   fal.ai 托管（短片）
│   │   └── fake_engine.py           #   无 GPU 调试引擎
│   └── postprocess/wav2lip.py       #   可选口型精修
├── server/                          # ★ FastAPI 服务
│   ├── app.py                       #   POST /api/v1/jobs 等接口
│   └── jobs.py                      #   后台任务队列 + 状态持久化
├── scripts/start_api.sh             # 一键启动 API
├── examples/                        # Python / curl 调用示例
├── tests/                           # 单元 + 集成测试（fake 引擎，无 GPU 可跑）
├── roleswap/ + web/                 # legacy：旧的远程工作流封装
└── pyproject.toml / requirements.txt / .env.example
```

## 环境要求

- Python **3.10+**；ffmpeg / ffprobe
- 自托管推理：CUDA **11.8+**，显存 ≥24GB（`int8_convrot`）/ ≥32GB（`bf16`）
- ComfyUI 可部署在另一台 GPU 机器，通过 `COMFYUI_URL` 远程调用
- 走 `dashscope` 引擎则**不需要 GPU**，只要一个百炼 API Key

## 安装与启动

```bash
# 1) 安装（GPU 机器加 --with-comfyui 下载 Wan2.2-Animate 全套模型 ~40GB）
./setup.sh --with-comfyui
# 显存 <32GB：ANIMATE_VARIANT=int8_convrot ./setup.sh --with-comfyui
# 想同时装 SCAIL-2 备选引擎：./setup.sh --with-scail2

# 2) 配置
cp .env.example .env        # 默认 SCAILSWAP_ENGINE=wan_animate

# 3) 启动推理端（GPU 机器）
python3 ComfyUI/main.py --listen 127.0.0.1 --port 8188

# 4) 一键启动 API 服务
./scripts/start_api.sh      # 默认 0.0.0.0:8000，Swagger 文档在 /docs
```

**AutoDL 部署**（磁盘规划、aria2 手动下模型、端口与排错）见 **[DEPLOY_AUTODL.md](DEPLOY_AUTODL.md)**。
## 使用

### Python 一行调用

```python
from scailswap import swap_character

swap_character(
    source_image="face.jpg",          # 源角色照片
    target_video="talk.mp4",          # 口播视频，时长无上限
    output_path="final.mp4",
    prompt="一位穿深色衬衫的男士坐在办公室里对着镜头讲话，背景是书架",
    target_fps=16,                    # 生成帧率：Wan 原生 16fps，帧数减半
    output_fps=30,                    # 成片帧率：升回 30fps
    on_progress=lambda e: print(f"[{e.percent:5.1f}%] {e.message}"),
)
```

### HTTP API

```bash
# 提交任务
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -F "source_image=@face.jpg" \
  -F "target_video=@talk.mp4" \
  -F "prompt=一位穿深色衬衫的男士坐在办公室里对着镜头讲话" \
  -F "target_fps=16" -F "output_fps=30" \
  -F 'params_json={"seed": 42, "resolution_tier": 512}'
# → {"job_id": "a1b2c3...", "status": "queued"}

# 轮询进度（percent 0~100，含当前块序号与阶段）
curl http://127.0.0.1:8000/api/v1/jobs/<job_id>

# 完成后下载
curl -o final.mp4 http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

或用现成脚本：

```bash
python examples/test_api.py --image face.jpg --video talk.mp4 \
    --prompt "……" --output final.mp4
```

### 用百炼 API 测试 wan2.7（无需 GPU，实验性长视频）

```bash
# .env
SCAILSWAP_ENGINE=dashscope
DASHSCOPE_API_KEY=sk-xxxx
DASHSCOPE_MODEL=wan2.7-videoedit     # 参考级弱锚定，可跑长视频
DASHSCOPE_PUBLIC_BASE_URL=https://your-cdn.example.com/scailswap  # 大视频建议配
DASHSCOPE_PUBLIC_ROOT=./data
```

`wan2.7-videoedit` 单次上限 10s，processor 会按上限自动切块，并把上一块输出的
末帧作为附加参考图传给下一块（最多 4 张）维持身份。**块边界可能有轻微跳变**，
详见 [DESIGN.md](DESIGN.md) 第 3 节的机制说明与局限。

若把 `DASHSCOPE_MODEL` 设为 `wan2.2-animate-mix`（换人，2~30s），它没有任何跨块
锚定接口，超过 30s 的素材会被**直接拒绝**而不是硬拼——独立分段拼接就是被排除的
"伪造拼接"。此时可加 `max_duration_seconds=25` 做短片验证。

## 核心参数

| 参数 | 默认 | 说明 |
| :-- | :-- | :-- |
| `prompt` | "" | 描述**替换后**的画面；写清角色外观与场景效果更好 |
| `mode` | `replacement` | `replacement`=换人（保留原场景）/ `animation`=动作迁移 |
| `target_fps` | 跟随源 | **生成帧率**。口播推荐 16（Wan 原生训练帧率，帧数减半） |
| `output_fps` | = `target_fps` | **成片帧率**。可降帧生成后升回 30 |
| `interpolate_output` | `false` | 升帧率时用 `minterpolate` 运动补偿插帧（顺滑但慢） |
| `window_frames` / `overlap_frames` | 跟随引擎 | 分块窗口与锚定重叠（Animate 77/5，SCAIL-2 81/5），不建议改 |
| `steps` / `cfg` / `shift` | 6 / 1.0 / 5.0 | lightx2v 蒸馏 LoRA 下的采样配置 |
| `seed` | 随机 | 固定后全部分块共用 |
| `resolution_tier` | 512 | 512p / 704p（按源宽高比自动取 32 倍数） |
| `blend_curve` | `cosine` | 重叠区融合曲线：`cosine` / `gaussian` |
| `color_match` | `true` | Reinhard-LAB 逐块颜色对齐 |
| `cleanup_intermediate` | `true` | 块完成即删驱动分块（长素材必开） |
| `chunk_codec` | `lossless` | 分块中间编码；`crf` 省约 70% 磁盘 |
| `enable_wav2lip` | `false` | Wav2Lip 口型精修后处理 |
| `max_duration_seconds` | 空 | 只处理前 N 秒（调试） |
| `video_object` / `image_object` | `person` | SAM3 开放词汇跟踪目标 |

## 稳健性设计

- **断点续传**：分块状态写入 `work_dir/state.json`，中断后重跑自动跳过已完成块。
- **OOM 自动重试**：检测到显存溢出时调用 ComfyUI `/free` 卸载模型 + 清缓存
 （等价 `torch.cuda.empty_cache()`），指数退避后重试（默认 3 次）。
- **逐块显存清理**：每块完成后立即 `/free`，长任务显存不累积。
- **进度回调**：`ProgressEvent{percent, stage, message, chunk_index}` 贯穿
  准备→逐块生成→拼接→音频→后处理，API 侧可实时轮询。
- **磁盘管理**：驱动分块与引擎原始输出用完即删，5 分钟素材不会堆出几十 GB。

## 开发

```bash
uv sync
uv run pytest tests/ -q     # 全链路测试用 fake 引擎，无 GPU 可跑
uv run ruff check .
```
