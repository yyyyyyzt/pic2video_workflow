# 数字人视频生成

两套东西：

1. **[数字人平台训练素材实验台](#数字人平台训练素材实验台)**（`avatar_lab.py`）——生成用来喂给
   **腾讯云数智人**平台做形象定制的口播素材。当前主线是 **腾讯通用口型版**
   （1–10 分钟口播、1080P、开头静默闭口）。横向跑多个 API 方案，自动 TTS、超分、
   规格规范化和入库校验。**新需求看这一节。**
2. **[可灵数字人](#可灵数字人视频生成)**（`kling_avatar.py` / `server.py`）——最初的单供应商版本，
   出成片用。历史快照在 `archive/kling-console` 分支。

---

# 数字人平台训练素材实验台

## 这一节要解决的问题

目标不是「生成一条好看的视频」，而是**生成一条平台肯收、且训练出来的形象够自然的素材**。
真人拍摄的问题在于没有镜头感、表情僵硬、口误重拍，一分钟的口播能耗掉一小时还拍不好；
而需要经常换人时，这个成本要乘以人数。用 AI 生成素材可以绕开这些，
但换来一个新约束：**平台在「系统检测」环节会按硬指标退料**。

这一点很容易踩坑，因为绝大多数数字人 API 默认输出 480p/720p、25fps，而平台最低要 1080P。
所以 `avatar_lab.py` 跑的不是单纯的「生成」，而是完整一条：

```
上传素材 → 生成 → （可选）超分到 1080p → 规范化(比例/帧率/容器) → 入库合规校验 → 汇总
```

10 秒筛选不要一条条改参数。内置矩阵一次跑完所有组合，对照页里逐条看：

```bash
python3 avatar_lab.py sweep --image face.jpg
```

`入库校验`是本地预检腾讯/阿里「系统检测」的硬指标（时长、短边、比例、帧率、封装、文件大小、开头 2 秒音量 ≤ -45dB），**不是生成失败**。10 秒筛选成片过不了 60 秒门槛，这是故意的；实时日志会逐条打出未过项。

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

## 准备

```bash
pip install -r requirements.txt
apt install ffmpeg          # 或 brew install ffmpeg，compliance/mediaprep 依赖它

cp .env.example .env
# 填 WAVESPEED_API_KEY=wsk_live_xxxx（海外，模型最全）
# 国内通道另填 MOARK_API_KEY（模力方舟，有 InfiniteTalk 和 Duix-Avatar）

python3 avatar_lab.py balance      # 确认 key 通了
```

素材准备一张**正面清晰、五官无遮挡、嘴巴闭合**的角色图即可。
驱动音频用 TTS 合成，不必自己录：仓库里已经写好 10 秒筛选稿和 60 秒入库稿。

## 关于花钱，先看这里

WaveSpeed 的 `base_price` **单位因模型而异**，官方文档也写了「以实际扣费为准」。
本仓库已实测两个：

| 模型 | base_price | 实测真实单价 | 60 秒成本 |
| :-- | --: | --: | --: |
| SkyReels V3 标准版 | 0.04 | **$0.044 / 秒**（即每秒计价） | ≈ $2.6 |
| 字节视频超分 → 1080p | 0.0072 | **$0.008 / 秒** | ≈ $0.5 |

对比之下 InfiniteTalk 的 `base_price` 同样是 0.15，但官方描述写的是「720p tier
$0.30/**5s**」——两者差 5 倍。所以工具不靠公式猜，而是**在每次调用前后各查一次余额，
用差额算出真实成本**，写进 `data/cost_calibration.json`（按累计花费÷累计秒数加权，
因为余额只精确到分，短任务算出来的单价会偏高）。`plan` 输出里带 `?` 的是还没实测过的
粗估，**可能偏差数倍**，跑过一次就自动变准。

**你现在这个 key 只有 $1**，实测下来跑一条 60 秒的 SkyReels + 超分就要约 $3。
所以下面的步骤按「先花几毛钱筛方案，再花几美元出成片」来排。
要跑完整轮对比，建议先充 $20~30。

## 执行步骤

### 第 0 步：验证连通性（免费）

```bash
python3 avatar_lab.py balance                 # 查余额
python3 avatar_lab.py list --seconds 60       # 看全部方案和 60 秒成本
python3 avatar_lab.py models --filter avatar  # 查 WaveSpeed 上还有什么模型
```

### 第 1 步：本地链路自检（免费，不调 API）

先确认 ffmpeg 那套能用，免得 API 跑完了卡在最后一步：

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

跑完用浏览器打开 `data/lab/sweep-screen10/index.html`。页面上每条都有视频和开头 0.3/1/2/3 秒抽帧，可按模型/静默/提示词筛选。
中途余额不够或失败，同样命令再跑会跳过已完成的格子。

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

### 第 4 步：赢家跑完整 60 秒（约 $1.5~3）

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

## 国内通道（模力方舟）

节点在国内，同时有 InfiniteTalk 和 Duix-Avatar，适合对海外网络或合规有顾虑的场景：

```bash
python3 moarkclient.py models --filter avatar    # 免鉴权，先看有什么
python3 moarkclient.py probe InfiniteTalk --image <url> --audio <url>
```

需要说明的是，模力方舟这两个模型的确切入参、时长上限和单价，官网未登录抓不到
（模型列表是前端动态加载的），必须登录控制台在模型体验页看「API」示例。
`probe` 子命令的作用就是拿真实报错反推字段名，据此校准 `moarkclient.py` 里的 payload。

## 命令速查

| 命令 | 作用 |
| :-- | :-- |
| `avatar_lab.py balance` | 查余额 |
| `avatar_lab.py list [--seconds N]` | 列出全部方案与成本 |
| `avatar_lab.py plan --recipes ... --seconds N` | 估算成本，不花钱 |
| `avatar_lab.py sweep --image face.jpg` | **10 秒参数矩阵，一次跑完对照试验** |
| `avatar_lab.py run --recipes ...` | 跑单组实验（完整流水线，给 60 秒赢家用） |
| `avatar_lab.py report <目录>` | 重新汇总已有结果 |
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

---

# 可灵数字人视频生成

基于**可灵 AI 数字角色 2.0** 官方 API：**一张角色图** + **一段音频（或一段台词自动配音）** + **一句提示词**，直接生成口型、表情、动作俱全的数字人视频。

提供两种用法：**网页控制台**（上传、TTS 配音、看任务进度）和**命令行**。

不再需要自托管 GPU、模型下载、分块拼接——官方接口单次即支持最长 5 分钟内容（音频驱动模式单段音频上限 60 秒，正好覆盖 60 秒测试）。

- 使用指南：[数字角色 2.0 使用指南](https://docs.qingque.cn/d/home/eZQCNHbAH5WUzp1SCYw0uTUcQ?identityId=2MueRKz7Jhc)
- API 文档：[数字人接口](https://klingai.com/document-api/api/video/avatar)

## 网页控制台（推荐）

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入密钥，见下方「快速开始」第 2 步
python3 server.py           # 打开 http://127.0.0.1:8000
```

界面上可以：

- 拖入**角色图**（自带预览）
- 配音二选一：**上传音频**，或**填台词自动 TTS 配音**（14 种音色、可调语速、能试听）
- 从 `prompts/` **一键载入提示词模板**（台词稿会自动载入到台词框，不会混进提示词）
- 选 `std` / `pro`，可勾选生成后自动跑**抖动后处理**
- 右侧看**任务进度**：阶段（配音 → 提交 → 生成 → 下载 → 后处理）、已耗时、实时日志，完成后直接在页面里播放和下载

任务串行执行以免打爆 API 并发额度；状态存在 `data/jobs.json`，服务重启后历史仍在，未完成的会继续轮询。无登录鉴权，默认只监听 `127.0.0.1`，需要内网访问再加 `--host 0.0.0.0`。

```bash
python3 server.py --host 0.0.0.0 --port 9000
```

## 快速开始（命令行）

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 配置密钥
cp .env.example .env
# 新版控制台拿到的是单个 api-key-kling-xxx → 填 KLING_API_KEY
# 旧版才是 AccessKey + SecretKey 两把不同的钥匙 → 填 KLING_ACCESS_KEY / KLING_SECRET_KEY

# 3) 一条命令生成测试视频（提示词建议用文件，方便反复改）
python3 kling_avatar.py \
    --image face.jpg \
    --audio speech.mp3 \
    --prompt-file prompts/electricity_safety_recommended.txt \
    --mode std \
    --output result.mp4
```

提示词文件放在 `prompts/`，以 `#` 开头的行是注释会被忽略。先读 `prompts/_guide.txt` 了解哪些内容写进提示词有用、哪些没用。同一音频下可用推荐版 / 极简版 / 完整版做 A/B：

```bash
python3 kling_avatar.py --image face.jpg --audio speech.mp3 \
    --prompt-file prompts/electricity_safety_ultrashort.txt -o result_short.mp4
python3 kling_avatar.py --image face.jpg --audio speech.mp3 \
    --prompt-file prompts/electricity_safety_full.txt -o result_full.mp4
```

不想自己准备音频，可以直接给台词让脚本走 TTS：

```bash
python3 kling_avatar.py --image face.jpg \
    --script-file prompts/electricity_safety_script.txt \
    --voice genshin_vindi2 --voice-speed 1.0 \
    --prompt-file prompts/electricity_safety_recommended.txt -o result.mp4

python3 kling_avatar.py --list-voices     # 查看可用音色
```

脚本会自动完成：本地文件转 Base64 →（可选 TTS 配音）→ 提交任务 → 轮询状态 → 下载成片。60 秒素材通常十几分钟内完成。

## 参数说明

| 参数 | 必填 | 说明 |
| :-- | :-- | :-- |
| `--image` | 是 | 角色图，本地文件或 URL。jpg/jpeg/png，≤10MB，宽高 ≥300px，宽高比 1:2.5~2.5:1 |
| `--audio` | 四选一 | 驱动音频，本地文件或 URL。mp3/wav/m4a/aac，≤5MB，**时长 2~60 秒**（口型跟随音频） |
| `--audio-id` | 四选一 | 已有的可灵音频 ID（30 天内有效） |
| `--script` / `--script-file` | 四选一 | 台词文本，自动走 TTS 合成配音（单次 ≤1000 字），配 `--voice` / `--voice-speed` |
| `--prompt` / `--prompt-file` | 否 | 提示词；文件版方便编辑测试（`#` 行是注释）。≤2500 字符。**台词不要写进提示词**，口型跟着音频走 |
| `--mode` | 否 | `std` 标准模式（性价比高，默认）/ `pro` 专家模式（质量更高） |
| `--output` | 否 | 输出路径，默认 `avatar_output.mp4` |

视频时长 = 音频时长，所以**准备一段约 60 秒的音频即可得到约 60 秒的视频**。

## 提示词启发

提示词只需描述「角色怎么表演」，官方推荐从**动作、情绪、眼神/镜头**三个维度写。以下模板可直接套用：

**口播 / 知识讲解**
> 耐心、温柔地讲解，保持微笑，时不时看向镜头，偶尔用手势辅助说明，动作自然

**新闻 / 正式播报**
> 端正坐姿，眼神专注自信地看着镜头，语气沉稳，偶尔轻微点头强调重点，动作克制自然

**带货 / 产品介绍**
> 一手拿着产品面向镜头介绍，表情热情有感染力，讲到卖点时把产品微微抬向观众，手势明确自信

**活泼 / 短视频风格**
> 一边说话一边兴奋地摇头晃脑，表情丰富，讲到关键处伸手比划，最后握拳鼓劲，整体轻快有活力

**唱歌 / 表演**
> 眼神专注沉醉地唱歌，手持麦克风，手臂随节奏自然摆动，偶尔看向镜头微笑，固定镜头

**情绪戏 / 角色扮演**
> 先平静叙述，说到中途忽然想起什么，轻微皱眉露出委屈的表情，随后叹气摇头

**画面要稳（减少人物位置顿挫）**
> 固定三脚架镜头，完全静止机位，无推拉摇移。人物端坐口播，躯干基本不动，只有口型、
> 微表情和偶尔小幅手势，动作缓慢平滑，不要大幅度身体位移，背景保持稳定。

写作技巧：

1. **动词要具体**：「摇头晃脑」「扶了一下眼镜」「双手合十」比「动作自然」更有效；
2. **给情绪曲线**：「先……忽然……最后……」这类时序描述能带出表演层次；
3. **控制镜头**：加「固定镜头」「镜头缓缓推进」可以约束运镜；
4. **求稳就压幅度**：少写大位移动词，配合 `--mode pro`，再用下面的后处理收尾；
5. **中英文都支持**，不超过 2500 字符。

## 后处理：消除「前后两帧人物位置顿一下」

生成结果偶尔出现人物位置微跳，是模型的**时序微抖动**，提示词只能软约束、压不掉。
`stabilize.py` 提供几种可独立测试的后处理方法，全部**保留原音轨、不改帧数**（音画不会漂移）。

```bash
pip install -r requirements.txt   # 需要 opencv-python-headless + numpy
# 另需系统装 ffmpeg：apt install ffmpeg / brew install ffmpeg
```

### 第一步：先诊断，别急着修

```bash
python3 stabilize.py result.mp4 --method analyze
```

它不修改视频，只输出抖动指标，并列出**最抖的时刻**（可直接跳到那一秒逐帧看）：

```
帧间位移（像素/帧）  中位数 0.414   p95 7.549
抖动加速度（像素/帧²）中位数 0.232   p95 7.131   p99 8.132   最大 8.389

尖峰比（p99/中位数）= 35.1
  → 存在明显孤立尖峰，正是你说的「偶尔顿一下」。建议 --method track

最抖的 6 个时刻：
   1. 第    37 帧  t =   1.23s   加速度  7.579
   2. 第    71 帧  t =   2.37s   加速度  8.389
```

它会自动区分两种性质完全不同的抖动，并**直接打印出该跑的命令**（含按实测尖峰幅度算出的
`--max-shift`）：

- **孤立突跳**：只有极少数帧异常，其余本来就平顺。常表现为加速度上相邻两帧同时超标，
  即「跳出去又跳回来」的单帧位置突跳。→ 用**中值滤波**精准打击。
- **持续性抖动**：异常不集中、全程都在轻微晃。→ 用**高斯低通**。

这个区分很关键：孤立突跳用高斯会把整段视频的自然微动一起抹平，人物看起来会发僵。

### 第二步：按方法逐个试

```bash
# ★ 推荐：光流轨迹平滑 + 整帧仿射补偿（不影响口型与表情）
python3 stabilize.py result.mp4 -o fixed_track.mp4 --method track

# 通用稳像，强度更猛（可能把真实的头部动作也削掉）
python3 stabilize.py result.mp4 -o fixed_vidstab.mp4 --method vidstab

# 只治亮度闪烁，不治位移
python3 stabilize.py result.mp4 -o fixed_deflicker.mp4 --method deflicker

# 运动补偿插帧到 60fps：不消除抖动，但观感更顺
python3 stabilize.py result.mp4 -o fixed_interp.mp4 --method interp

# 串联：先稳位置再压闪烁
python3 stabilize.py result.mp4 -o fixed_chain.mp4 --method track,deflicker

# 生成左右并排对比视频，肉眼确认是否值得
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --compare compare.mp4
```

**实测一：持续性抖动**（180 帧，全程注入抖动 + 5 次跳变）

| 方法 | 中位数 | p95 | p99 | 最大 | 结论 |
| :-- | --: | --: | --: | --: | :-- |
| 原始 | 0.232 | 7.131 | 8.132 | 8.389 | 基准 |
| **track** | 0.052 | 0.187 | 0.411 | 0.960 | **p99 降 95%，最有效** |
| vidstab | 0.326 | 1.702 | 2.511 | 3.264 | 有效但不如 track |
| deflicker | 0.238 | 7.142 | 8.141 | 8.410 | 对位移几乎无作用（符合预期） |
| interp | 0.037 | 0.298 | 3.925 | 4.231 | 观感变顺，尖峰仍在 |
| track,deflicker | 0.052 | 0.168 | 0.394 | 0.924 | 与 track 相当 |

**实测二：孤立突跳**（900 帧竖屏，98.9% 帧平顺 + 5 处成对突跳，最贴近真实数字人输出）

| 方案 | 中位数 | p95 | p99 | 最大 | 残余尖峰 |
| :-- | --: | --: | --: | --: | --: |
| 原始 | 0.234 | 0.761 | 7.503 | 19.067 | 5 处 |
| **median r5 --max-shift 28** | 0.201 | 0.743 | 0.999 | 1.666 | **0 处** |
| hybrid r3 --max-shift 28 | 0.067 | 0.208 | 0.290 | 0.434 | 0 处 |
| gaussian r15 --max-shift 12 | 0.054 | 0.190 | 0.419 | 7.099 | **5 处（没修掉）** |
| gaussian r15 --max-shift 28 | 0.056 | 0.182 | 0.241 | 0.715 | 1 处 |

两个要点：

1. **`--max-shift` 不够大会白干**。上表里 `--max-shift 12` 那行，5 处尖峰一个都没消除——
   因为消除它们需要约 19px 的补偿，被限幅削掉了。`analyze` 会算出你的视频需要多少并直接建议。
2. **median 保住了自然感**。它把最大尖峰降了 91% 而中位数只从 0.234 降到 0.201（自然微动保留约 86%）；
   gaussian 虽然数字更漂亮，但中位数被压到 0.054，等于把人物的自然微动也抹掉了。

### 第三步：track 效果不理想时调参

```bash
# 尖峰没消尽（运行时提示「补偿被 --max-shift 限制」）→ 放宽限幅
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --max-shift 40

# 还能看出残留 → 从 median 升到 hybrid（中值去脉冲 + 轻度高斯收尾）
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --smooth-mode hybrid --radius 3

# 全程都在轻微晃（不是孤立突跳）→ 换高斯低通并加大窗口
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --smooth-mode gaussian --radius 20

# 人物动作被削平、显得发僵 → 回到 median 或减小窗口
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --smooth-mode median --radius 5

# 只修上下左右位移，不动缩放和旋转（最保守，画面最不易变形）
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --lock-scale --lock-rotation

# 只用人物所在区域估计运动（背景很杂或有动态背景时更准）
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --roi upper
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --roi 0.25,0.05,0.5,0.7

# 边缘出现拉伸/镜像痕迹 → 加大裁切；完全不想放大 → --zoom 1.0
python3 stabilize.py result.mp4 -o fixed.mp4 --method track --zoom 1.04
```

主要参数：

| 参数 | 默认 | 说明 |
| :-- | :-- | :-- |
| `--smooth-mode` | `median` | `median` 只去孤立突跳（保自然感）/ `hybrid` 中值+轻度高斯 / `gaussian`、`box` 传统低通 |
| `--radius` | 15 | 平滑窗口半径（帧）。median 对它不敏感；gaussian 越大越稳但真实运动越被削弱 |
| `--max-shift` | 30 | 单帧最大补偿位移（像素），`0` 为不限制。**太小会让大尖峰修不掉** |
| `--roi` | `full` | 运动**估计**区域：`full` / `upper` / `center` / `x,y,w,h`。补偿始终作用于整帧 |
| `--zoom` | 1.02 | 轻微放大以裁掉补偿产生的边缘 |
| `--lock-scale` / `--lock-rotation` | 关 | 只修平移，最保守 |
| `--crf` | 17 | 输出质量，17 约等于视觉无损 |

### 生成时顺手做掉

```bash
python3 kling_avatar.py --image face.jpg --audio speech_60s.mp3 \
    --prompt "固定机位，人物端坐口播，躯干基本不动，动作缓慢平滑" \
    --output result.mp4 --stabilize track
```

原始成片会保留为 `result_raw.mp4`，方便和处理后的版本对照；后处理失败会自动还原，不留半成品。

### 需要知道的取舍

- `track` 的补偿是**整帧**的，所以口型、表情等局部形变完全不受影响——这是它比通用稳像更适合口播的原因；
- 代价是：若背景本来完全静止，补偿会让背景反向轻移。补偿量通常只有几像素，实际难以察觉，但如果背景有明显直线（门框、书架）可以改用 `--lock-scale --lock-rotation` 降低可感知度；
- 后处理救不了**内容级崩坏**（变脸、手指错乱、明显跳变）。那种情况请回到提示词与参考图，重新生成更划算；
- **孤立突跳靠改提示词解决不了**。如果 `analyze` 显示整体运动平顺（p95 很小）、只有几处尖峰，
  说明动作幅度本来就没问题，突跳更像模型在长视频内部的时序衔接处产生的，提示词管不到这一层。
  长视频重抽成本高、且尖峰位置随机，不保证变少——优先用后处理修。
  「锁镜头 + 小动作」的提示词更适合用作**新项目的预防**，而不是为这个问题返工重抽。

## 项目结构

```
klingclient.py   API 核心：鉴权、媒体编码、数字人任务、TTS（CLI 与 Web 共用）
kling_avatar.py  命令行入口
server.py        FastAPI Web 控制台（任务队列 + 进度 + 持久化）
web/index.html   前端单页（原生 JS，无构建步骤）
stabilize.py     抖动后处理（诊断 + 多种方法）
prompts/         可编辑提示词模板与台词稿
data/            运行数据：uploads / outputs / jobs.json（已 gitignore）
```

## 底层接口（脚本封装的内容）

```
POST {base}/v1/videos/avatar/image2video      # 创建任务
     body: { image, sound_file | audio_id, prompt?, mode? }
GET  {base}/v1/videos/avatar/image2video/{id} # 查询任务（succeed 后含视频 URL）
POST {base}/v1/audio/tts                      # 文本转语音，同步返回 audio_id
     body: { text, voice_id, voice_language, voice_speed }
```

鉴权支持两种：

1. **新版单 Key**（`KLING_API_KEY=api-key-kling-...`）：直接 `Authorization: Bearer <key>`；
2. **旧版 AK/SK**：用 SecretKey 签 JWT（HS256，30 分钟），再 `Bearer <jwt>`。

若把同一个 `api-key-kling-xxx` 同时填进 AK 和 SK，脚本会自动按单 Key 处理。

国内域名 `api-beijing.klingai.com`，海外 `api-singapore.klingai.com`，通过 `.env` 中 `KLING_API_BASE` 切换。

## 常见问题

- **401 `access key not found`**：你拿到的多半是新版单 Key。请填 `KLING_API_KEY`（不要用 JWT）。连通性本身没问题，是鉴权模式选错了；
- **401 / 区域不匹配**：国内密钥走 `api-beijing`，海外走 `api-singapore`，两边密钥不通用；
- **音频被拒**：检查时长是否在 2~60 秒之间、文件是否 ≤5MB、格式是否为 mp3/wav/m4a/aac；
- **图片被拒**：检查宽高 ≥300px、宽高比在 1:2.5~2.5:1 之间、是否 ≤10MB；Base64 不能带 `data:` 前缀（脚本已处理）；
- **任务排队久**：高峰期 `submitted/processing` 状态可能持续较长，脚本默认等待 1 小时，可用 `--timeout` 调整。
