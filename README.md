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
