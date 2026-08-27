# 数字人平台训练素材实验台

生成用来喂给 **腾讯云数智人**平台做形象定制的口播素材。主线是 **腾讯通用口型版**
（1–10 分钟口播、1080P、开头静默闭口）。横向跑多个 API 方案，自动 TTS、超分、
规格规范化、入库校验，再用一套客观指标给成片打分。

早期的单供应商版本（可灵 Web 控制台 `server.py` / `kling_avatar.py`、MiniMax H3 串接）
已从主线移除，保留在历史快照分支：

| 分支 | 内容 |
| :-- | :-- |
| `archive/kling-minimax-snapshot` | 移除前的完整主线快照（含可灵 CLI + Web 控制台） |
| `archive/kling-console` | 更早的可灵 Web 控制台版本 |
| `minimax-ai` | MiniMax H3 长视频串接研究 |

## 这一节要解决的问题

目标不是「生成一条好看的视频」，而是**生成一条平台肯收、且训练出来的形象够自然的素材**。
真人拍摄的问题在于没有镜头感、表情僵硬、口误重拍，一分钟的口播能耗掉一小时还拍不好；
而需要经常换人时，这个成本要乘以人数。用 AI 生成素材可以绕开这些，
但换来一个新约束：**平台在「系统检测」环节会按硬指标退料**。

这一点很容易踩坑，因为绝大多数数字人 API 默认输出 480p/720p、25fps，而平台最低要 1080P。
所以 `avatar_lab.py` 跑的不是单纯的「生成」，而是完整一条：

```
上传素材 → 生成 → （可选）超分 → 规范化(比例/帧率/容器)
→ 入库合规校验 → 客观指标打分 → 汇总
```

10 秒筛选不要一条条改参数。内置矩阵一次跑完所有组合，对照页里逐条看：

```bash
python3 avatar_lab.py sweep --image face.jpg
```

两道检查分工明确，不要混：

| | 问题 | 模块 | 性质 |
| :-- | :-- | :-- | :-- |
| **入库校验** | 平台**收不收** | `compliance.py` | 二值，标准来自腾讯/阿里文档 |
| **客观指标** | 素材**好不好** | `videometrics/` | 连续值，标准要用你的主观选择校准 |

「入库校验不通过」**不是生成失败**——10 秒筛选成片过不了 60 秒时长门槛，这是故意的，
日志会注明是筛选轮次的预期结果。

客观指标沿用 AGI-Eval《2026 数字人生成评测报告》的四维骨架（合理性/协调性/稳定性/一致性），
另加一维「活性」处理静默段。它**刻意不出总分**，只给指标向量 + 带时间戳的缺陷清单。
设计依据、已知局限和校准流程见 **[docs/EVALUATION.md](docs/EVALUATION.md)**。

## 平台入库硬指标

跑之前先确认你要投的是哪一档，因为要求差别很大（`compliance.py` 里已内置这五档）：

| 档位 | 时长 | 分辨率 | 帧率 | 素材内容 |
| :-- | :-- | :-- | :-- | :-- |
| `tencent-broadcast` 腾讯·播报场景 | ≥30 秒 | 1080P / 4K | 25–60 | 口播 |
| `tencent-general` 腾讯·通用口型 | 1–10 分钟 | 1080P / 4K | 25–60 | 口播 |
| `tencent-hifi` 腾讯·高精版 | 2–10 分钟 | **必须 4K** | 25–60 | 口播 |
| `aliyun-fewshot` 阿里·2D 小样本 | **10 秒–2 分钟** | 1080P+ | **≥30** | **全程闭嘴，不说话** |
| `aliyun-hifi` 阿里·2D 高精度 | 5 分钟 | 1080P+ | ≥30 | 口播 |

共同要求：宽高比严格 16:9 或 9:16、mp4/mov、全程无剪辑无跳帧、人脸清晰正视镜头无遮挡、
**开头静默闭口 1–3 秒**。

两个值得注意的点：

- **当前主线是腾讯通用口型版**（`--profile tencent-general`）。腾讯官方明确支持用 AI
  生成素材，它的[播报场景录制指引](https://cloud.tencent.com/document/product/1240/103864)里
  写了「您可以通过 AI 生成技术快速生成一段约一分钟的人物视频，在数智人平台上训练和使用」。
- 阿里云 2D 小样本版（全程闭嘴、10 秒起）也能跑，但口播自然度主观上通常不如通用口型版；
  需要时用 `--profile aliyun-fewshot` 和路线 B，不是默认路径。

## 四条路线

`python3 avatar_lab.py list` 可以看到全部 28 个方案和单价。

| 路线 | 做法 | 什么时候用 |
| :-- | :-- | :-- |
| **A 单图直出** | 一张图 + 音频 → 说话视频 | 默认起点。要频繁换人时最省事 |
| **B 静默素材** | 一张图 → 10~15 秒闭嘴微动视频 | 投阿里云小样本版。最便宜、最稳 |
| **C 角色替换** | 模板视频 + 图/音频 | 要 60 秒以上且必须绝对稳定时 |
| **U 超分收尾** | 任意视频 → 1080p / 4K | A 和 C 基本都要，因为平台最低 1080P |

## 两个入口，给不同的人用

| | 谁用 | 干什么 |
| :-- | :-- | :-- |
| `studio.py` | **运营** | 网页上换提示词、调秒数、单条试效果 |
| `avatar_lab.py` | 工程 | 批量矩阵、可复现对比、出入库素材 |

调试台启动方式（运营只需要这两行）：

```bash
pip install -r requirements.txt
python3 studio.py            # 打开 http://127.0.0.1:8000
```

想让同事从别的机器访问：`python3 studio.py --host 0.0.0.0`。

页面上做了这几件事：

- **左右两栏**：左边表单（宽度封顶，下拉框不会被拉长），右边任务列表拿走剩余空间，
  宽屏下卡片并排显示方便横向对比。窄屏自动变上下单栏。
- **提示词拆成积木**：坐/站 / 手势 / 表情 / 体态 / 开口方式 / 禁止项各一个下拉框，
  机位和画质那些硬约束固定住不让改坏。手势有 8 种可选，不再只有摊手。
- **6 个预设**：基线、手不动、数点讲解、单手交替、播报站姿、语气坚定。
- **模型可多选**：勾几个就建几条任务，素材和提示词完全共用，只有模型不同，
  回来直接对比。默认按顺序一个个跑（并发会被限流，串行的实测扣费也才算得准）。
- **直接填秒数**，会换算成需要多少字，并能按句号把台词裁到目标长度。
- **只给下载不内联播放**：列表里嵌 `<video>` 会让浏览器自动预取每一条，带宽扛不住。
- **每条显示实测扣费**，选模型时标签上就写着单价。
- **失败可重连或重试**：远端任务号会记下来，本地网断了也能把已付费的结果捞回来。

### 坐姿 / 站姿怎么处理

运营常遇到「用户传了站姿照片但要坐着说」。**单图直出类模型改不了全身姿态**——
它们从你给的那一帧往下 animate，站着的人不会因为一句提示词就坐下。

所以分两步走：

1. 「坐/站」下拉框里的提示词，只在**会重新生成身体**的模型上有机会生效
   （`skyreels-std`、`skyreels-talking`、`pruna-avatar`、`omnihuman-15`）。
   勾的模型都不支持时，页面会直接提示「坐/站改不动」。
2. 要稳定换姿态，**先改照片**：用图像编辑模型（约 $0.02~0.03 一张）把参考图改成
   目标姿态，再拿改好的图去生成。`promptlib.PHOTO_EDITS` 备好了五条提示词
   （站→坐、坐→站、坐到桌前、把嘴改成闭合、转正面），每条都锁住了长相穿着光照。

细节和依据见 **[docs/PROVIDERS.md](docs/PROVIDERS.md)**。

## 目录结构

| 路径 | 职责 |
| :-- | :-- |
| `studio.py` | 提示词调试台（网页，给运营） |
| `promptlib.py` | 提示词积木与预设 |
| `tencentclient.py` | 腾讯云 TokenHub（调试台国内通道） |
| `moarkclient.py` | 模力方舟（Gitee AI，调试台已下架） |
| `avatar_lab.py` | CLI 入口：参数解析 + 调度 |
| `docs/PROVIDERS.md` | 供应商与模型调研（含 MuseTalk 能不能用 API） |
| `docs/MOARK.md` | 国内通道实测记录 |
| `recipes.py` | 28 个模型方案、计价、请求体组装 |
| `matrices.py` | sweep 的参数矩阵定义 |
| `compliance.py` | 平台入库硬门槛（能不能收） |
| `videometrics/` | 客观质量指标（好不好） |
| `mediaprep.py` | ffmpeg 封装：静默头、规范化、抽帧、循环 |
| `tts.py` / `wsclient.py` | TTS 与 WaveSpeed 客户端 |
| `lab/` | CLI 内部实现：表格、HTML 对照页、BT 排名 |
| `stabilize.py` | 抖动**修复**工具（和 videometrics 的只读打分不同） |
| `tests/` | pytest，离线部分不需要 API key |

## 准备

```bash
pip install -r requirements.txt
apt install ffmpeg          # 或 brew install ffmpeg，compliance/mediaprep 依赖它
apt install libegl1 libgles2  # Linux 上 mediapipe 需要，否则客观指标跑不了

cp .env.example .env
# 填 WAVESPEED_API_KEY=wsk_live_xxxx（海外，模型最全）
# 国内通道填 TOKENHUB_API_KEY（腾讯云 TokenHub，人像驱动 yt-video-humanactor）

python3 avatar_lab.py balance      # 确认 key 通了
```

素材准备一张**正面清晰、五官无遮挡、嘴巴闭合**的角色图即可。
驱动音频用 TTS 合成，不必自己录：仓库里已经写好 10 秒筛选稿和 60 秒入库稿。

## 关于花钱，先看这里

`base_price` **单位因模型而异**，官方文档也写了「以实际扣费为准」。已实测的：

| 模型 | base_price | 实测真实单价 | 计价单位 | 80 秒成本 |
| :-- | --: | --: | :-- | --: |
| 字节 OmniHuman 1.5 | 0.16 | **$0.156 / 秒** | 每秒 | **≈ $12.6** |
| SkyReels V3 标准版 | 0.04 | $0.044 / 秒 | 每秒 | ≈ $3.5 |
| InfiniteTalk 快速版 | 0.075 | $0.017 / 秒 | 每 5 秒块 | ≈ $1.4 |
| 字节视频超分 → 1080p | 0.0072 | $0.008 / 秒 | 每秒 | ≈ $0.7 |

规律：**`bytedance/*` 和 `skywork-ai/*` 按每秒，`wavespeed-ai/*` 按每 5 秒块。**

这个单位差异是真会花钱的。OmniHuman 1.5 一开始被当成「每 5 秒」，80 秒素材估出
$2.72，实际扣了 **$12.64，少报 4.6 倍**。所以现在没实测过的模型，`plan` 给的是
**区间**而不是一个数，余额护栏也按区间上界挡：

```
infinitetalk-720  $5.10~24.30?   ← 两端分别按「每 5 秒」和「每秒」算
合计 $23.31，最坏 $42.51
```

真实成本以每次调用前后的余额差为准，写进 `data/cost_calibration.json`
（按累计花费÷累计秒数加权，因为余额只精确到分，短任务算出的单价会偏高）。
跑过一次的模型，之后估算就准了。

**跑 80 秒入库素材之前一定先 `plan` 或 `--dry-run`。** 一条 OmniHuman 就是 $12.6。

## 执行步骤

### 第 0 步：验证连通性（免费）

```bash
python3 avatar_lab.py balance                 # 查余额
python3 avatar_lab.py list --seconds 60       # 看全部方案和 60 秒成本
python3 avatar_lab.py models --filter avatar  # 查 WaveSpeed 上还有什么模型
```

### 第 1 步：本地链路自检（免费，不调 API）

先把测试跑一遍，能覆盖计价、矩阵展开、入库校验边界、ffmpeg 流水线和全部客观指标：

```bash
python3 -m pytest -q
```

再确认 ffmpeg 那套能用，免得 API 跑完了卡在最后一步：

```bash
# 造一个故意不合规的样本：480×832、25fps、12 秒
ffmpeg -f lavfi -i "testsrc2=size=480x832:rate=25:duration=12" \
       -f lavfi -i "sine=frequency=400:duration=12" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest /tmp/fake.mp4

python3 compliance.py /tmp/fake.mp4 --profile tencent-general    # 应该报一堆不通过
python3 mediaprep.py normalize /tmp/fake.mp4 -o /tmp/fixed.mp4 --profile tencent-general
python3 compliance.py /tmp/fixed.mp4 --profile tencent-general   # 分辨率/比例/帧率应转为通过
```

### 第 2 步：TTS 合成 10 秒筛选音频（约 $0.02）

不要自己录 `speech_10s.mp3`。台词稿已经写好，走 WaveSpeed 的中文 TTS：

```bash
python3 tts.py --list-voices          # 看音色。默认 qwen:Cherry（亲和女声）

# 单独合成，开头自动加 2 秒静音（腾讯要求开头闭口 1–3 秒）
python3 tts.py --script-file prompts/tencent_general_script_10s.txt \
    -o speech_10s.mp3 --voice qwen:Cherry --silence 2

# 等价写法，挂在 avatar_lab 下面：
python3 avatar_lab.py tts --script-file prompts/tencent_general_script_10s.txt -o speech_10s.mp3
```

`run` 也可以不预先合成，直接把台词稿丢进去，它会先 TTS 再生成：

```bash
python3 avatar_lab.py run --recipes screen --image face.jpg \
    --script-file prompts/tencent_general_script_10s.txt
```

### 第 3 步：一条命令跑完 10 秒对比试验

不要再手工改 `--recipes` / `--silence` / `--prompt-file`。默认矩阵 `screen10` 会交叉：

| 轴 | 取值 |
| :-- | :-- |
| 模型 | SkyReels 标准、InfiniteTalk 快速、OmniHuman 1.5 |
| 静默头 | 2s（平台目标，先跑）、1s、3s |
| 提示词 | `alive`（静默时眨眼微动）、`strict`（更克制） |

共 18 条约 10 秒视频。音色固定 `seed:felix_zh`，台词固定 10 秒筛选稿，**不做静止帧替换、不超分**——这次要看的是模型自己怎么演开头。

```bash
python3 avatar_lab.py sweep --list
python3 avatar_lab.py sweep --image face.jpg --dry-run   # 只看格子和成本
python3 avatar_lab.py sweep --image face.jpg             # 开跑
```

跑完用浏览器打开 `data/lab/sweep-screen10/index.html`，两个页签：

- **网格对照**：每条的视频、开头 0.3/1/2/3 秒抽帧、客观指标、缺陷清单，可按模型/静默/提示词筛选
- **盲测**：一次只给两条、不显示模型名、左右随机，你选哪个好

中途余额不够或失败，同样命令再跑会跳过已完成的格子。

盲测投完点「导出 votes.json」放回结果目录，然后：

```bash
python3 avatar_lab.py rank data/lab/sweep-screen10
```

会给出 Bradley-Terry 排名，并把每个客观指标和你的偏好做 Spearman 秩相关——
相关系数接近 +1 的指标才有资格替你排序。这是整套评价体系唯一的有效性证明方式。

想先只看三个模型本身（3 条，静默 2s + alive）：

```bash
python3 avatar_lab.py sweep --image face.jpg --matrix screen10-models
```

对照时重点看：开头有没有深吸气、会不会眨眼、开口是不是从闭嘴直接开始、像不像本人。
挑几条满意的，把格子名（如 `01-skyreels-std_sil2_alive`）或日志贴回来再收窄。

单条复跑仍可用 `run`（给 60 秒赢家用）：

```bash
python3 avatar_lab.py run --recipes screen \
    --image face.jpg \
    --script-file prompts/tencent_general_script_10s.txt \
    --outdir data/lab/screen10s
```

### 第 4 步：赢家跑入库轮（`final60` 矩阵）

筛选定完模型后，用 `final60` 矩阵跑到平台要求的时长并补到 1080p：

```bash
python3 avatar_lab.py sweep --matrix final60 --image face.jpg --dry-run   # 先看成本
python3 avatar_lab.py sweep --matrix final60 --image face.jpg
```

台词稿实际约 80 秒（腾讯允许 1–10 分钟，留足余量防 TTS 语速波动），
三个模型加超分约 $13。**预算紧就一个一个跑**，音频和格子编号保持一致，
结果目录可以合并，对照页始终显示完整矩阵：

```bash
python3 avatar_lab.py sweep --matrix final60 --image face.jpg --only omnihuman
```

### 手工指定单条（老写法，仍可用）

```bash
python3 avatar_lab.py run \
    --recipes <第 3 步的赢家> \
    --image face.jpg \
    --script-file prompts/tencent_general_script_60s.txt \
    --prompt-file prompts/tencent_general_prompt.txt \
    --profile tencent-general --silence 2 \
    --outdir data/lab/final60s
```

60 秒台词稿按「加 2 秒静默头后刚好过 1 分钟」写的。`--silence 2` 默认就开着。
质量档组合也可以一次跑：`--recipes tencent`（OmniHuman 1.5 + InfiniteTalk 720p + 混元 Avatar + LTX 1080p）。

### 第 5 步：验证 120 秒会不会漂（约 $3~6）

InfiniteTalk 官方说 I2V 一分钟以内效果好，**超过 1 分钟色偏和身份保持会明显退化**。
你的目标是 60~120 秒，所以这条必须实测，不能靠推测。把 60 秒台词再念一遍拼成约 120 秒即可。

对比 `01_raw.mp4` 的第 10 秒和第 110 秒两帧，看肤色、服装颜色、五官是否还是同一个人。
如果明显漂了，转下一步的角色替换路线。

### 第 6 步：60 秒以上要绝对稳定，走角色替换（约 $2~6）

思路是把「长视频稳定性」从生成模型手里拿走：运动和背景来自一段真实模板视频，
生成模型只负责换脸和对口型。模板视频拍一次可以反复用，而且**不需要你有镜头感**——
它只提供身体动作，脸和声音都会被换掉。

```bash
python3 avatar_lab.py run --recipes animate-replace \
    --image face.jpg --video template_60s.mp4 \
    --profile tencent-general --outdir data/lab/replace

python3 avatar_lab.py run --recipes lipsync-2-pro \
    --video data/lab/replace/animate-replace/03_final.mp4 \
    --script-file prompts/tencent_general_script_60s.txt \
    --profile tencent-general --outdir data/lab/replace_lipsync
```

Wan2.2-Animate 常被认为只能做短片，但那是**阿里云百炼官方接口**的限制
（参考视频 2–30 秒）；WaveSpeed 这类第三方托管标称支持到 120 秒。
托管方自己加了免责声明说这是计费上限、不保证质量，所以值得实测一次。

### 第 7 步：交付前最后一道

```bash
python3 avatar_lab.py report data/lab/final60s
python3 compliance.py data/lab/final60s/*/03_final.mp4 --profile tencent-general
python3 stabilize.py data/lab/final60s/*/03_final.mp4 --method analyze
```

`analyze` 如果报出明显的孤立尖峰，用现有的 `stabilize.py --method track` 修一遍再交。
平台要求全程无剪辑，所以只能做整帧补偿，不能裁掉帧。

## 国内通道（腾讯云 TokenHub）

调试台国内选项走腾讯云 TokenHub 人像驱动（`yt-video-humanactor` / 1080p）。
在 `.env` 填 `TOKENHUB_API_KEY`。音频只要公网 URL：本地文件会借 WaveSpeed 的
上传接口中转，所以国内任务也需要能用的 `WAVESPEED_API_KEY`，或者自己先把
音频放到可访问的地址。

模力方舟问题太多，调试台下拉里已经拿掉。旧任务 `provider=moark` 仍能重连/重试。

## 命令速查

| 命令 | 作用 |
| :-- | :-- |
| `avatar_lab.py balance` | 查余额 |
| `avatar_lab.py list [--seconds N]` | 列出全部方案与成本 |
| `avatar_lab.py plan --recipes ... --seconds N` | 估算成本，不花钱 |
| `avatar_lab.py sweep --image face.jpg` | **10 秒参数矩阵，一次跑完对照试验** |
| `avatar_lab.py run --recipes ...` | 跑单组实验（完整流水线，给 60 秒赢家用） |
| `avatar_lab.py report <目录>` | 重新汇总已有结果 |
| `avatar_lab.py eval <视频>` | 单独跑客观指标（不花钱） |
| `avatar_lab.py rank <目录>` | 盲测投票 → BT 排名 + 指标相关性 |
| `avatar_lab.py tts` | 合成驱动音频（默认 10 秒筛选稿） |
| `tts.py --list-voices` | 列出中文音色 |
| `avatar_lab.py models --filter kw` | 查 WaveSpeed 模型清单 |
| `compliance.py <视频> --profile X` | 单独做入库校验 |
| `mediaprep.py normalize/silence/still-head/loop/mux` | 单独做媒体处理 |

`--recipes` 支持方案名、路线名（`A`/`B`/`C`/`U`）、`all`，逗号或空格分隔。
`run` 默认有 `--max-cost 1.0` 的护栏，超了要显式 `--yes`。

## 踩过的坑

- **别按 base_price 估成本**。单位不统一，SkyReels 是每秒、InfiniteTalk 是每 5 秒，
  实测差了 4 倍。以余额差额为准。
- **480p 出来的是 480×832，比例 1.733，不是标准 9:16**（1.778），平台会判不合格。
  `mediaprep.py normalize` 用「等比缩放 + 补边」而不是裁切来修，避免切掉头顶或下巴。
- **阿里云要求 ≥30fps，而多数模型输出 25fps**，必须重采样，腾讯的 25–60fps 则不用。
- **超分不是免费的**。60 秒 $0.60，虽然便宜但不是可忽略；原生 1080p 的方案
  （`ltx-lipsync`、`silent-seedance-1080`）会自动跳过这一步。
- **开头静默会被模型演成吸气**。给音频加 1–3 秒静音后，模型常在开口前深吸气。
  不要用静止帧去盖（不眨眼、很呆）。用 `sweep` 交叉「模型 × 静默时长 × 提示词」，
  看哪一组能在静默时眨眼、开口时不吸气。`--still-head` 仍可手动打开，默认关。
- **Bronze 账号限流 5 次/分钟、2 个并发**，方案是串行跑的，别改成并发。
