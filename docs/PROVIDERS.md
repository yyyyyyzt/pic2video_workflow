# 供应商与模型调研

回答一个具体问题：**除了现在用的，还有哪些数字人模型能通过 API 拿到。**

调研范围：WaveSpeed 全部 991 个模型（其中 47 个 `digital-human` 类）、
模力方舟 240 个模型，以及 GitHub 上开源方案实际用的模型。

## MuseTalk 能用 API 吗？不能，但有等价替代

[PunithVT/ai-avatar-system](https://github.com/PunithVT/ai-avatar-system)（418 star）
的技术栈是：**MuseTalk V1.5**（口型）+ Whisper（识别）+ Chatterbox（TTS 克隆）+ Claude（对话）。
它的 `AvatarAnimator` 只有两个引擎：`musetalk` 和 `simple`（ffmpeg 静图配音，无口型），
MuseTalk 靠 `scripts/inference.py` 本地推理，需要自备 GPU——
代码里明确写了没 GPU 就建议上 AWS g5/g6。

**MuseTalk 本身没有任何供应商提供 API**，它是自托管方案。但它属于「开源口型替换」
这一类，同类里有托管版：

| 模型 | 供应商 | base_price | 说明 |
| :-- | :-- | --: | :-- |
| `wavespeed-ai/latentsync` | WaveSpeed | 0.05 | **最接近 MuseTalk 的可用替代**，同为开源口型替换 |
| `bytedance/latentsync` | 字节 | 0.15 | 同一个开源模型的字节托管版，贵 3 倍 |
| `Duix-Avatar` | 模力方舟 | ¥0.01/秒 | 国内，实测 ¥0.1/秒。见 docs/MOARK.md |

结论：**不必自托管 MuseTalk**。要口型替换，`latentsync-ws`（$0.05）或国内的
Duix-Avatar 都能直接调，省掉一台 GPU 机器和运维。

## 新补进 recipes 的模型

翻完 WaveSpeed 全部 47 个 `digital-human` 模型后，补了这些能「单图 + 音频」直出的：

| recipe | 模型 | base | 为什么值得试 |
| :-- | :-- | --: | :-- |
| `pruna-avatar` | `pruna-ai/p-video/avatar` | **0.025** | 全表最便宜。有 `video_prompt` 可控体态 |
| `soulx-flashhead` | `wavespeed-ai/soulx-flashhead` | 0.075 | 音频支持到 30 分钟，长素材候选 |
| `ltx2-19b-lipsync` | `wavespeed-ai/ltx-2-19b/lipsync` | 0.1 | 原生 1080p，可省超分 |
| `skyreels-talking` | `wavespeed-ai/skyreels-v3/talking-avatar` | 0.15 | 19B，带体态 prompt，但**上限 20 秒** |

口型替换（路线 C，都要模板视频）：

| recipe | 模型 | base |
| :-- | :-- | --: |
| `latentsync-ws` | `wavespeed-ai/latentsync` | 0.05 |
| `veed-lipsync-v2` | `veed/lipsync-v2` | 0.075 |
| `bytedance-lipsync` | `bytedance/lipsync/audio-to-video` | 0.15 |
| `latentsync-bd` | `bytedance/latentsync` | 0.15 |

两个新组合：`--recipes cheap`（便宜档筛选）和 `--recipes lipsync`（口型替换横评）。

## HeyGen 有 API，但对我们没用

`heygen/avatar-v/digital-twin`（base 0.12）在 WaveSpeed 上可调，而且 HeyGen 正是
你给的那份 AGI-Eval 报告里**拟人度排第一**的模型（2.923 分）。

但它的 `avatar` 参数是一个**几百项的枚举**——只能从 HeyGen 预置的形象里选
（"Ann Doctor Sitting"、"Brandon Office Standing"、"Annie Sofa Sitting Front" 之类），
**不能上传自己的照片**。我们的需求是「经常换人」，所以它不适用。

一个副产品发现：HeyGen 预置形象的命名本身就区分 `Sitting` / `Standing`
（同一个人两套），说明**坐姿和站姿在业界是当成两个独立形象来做的，
而不是靠提示词从一张照片里变出来**。这印证了下面这条。

## 关于改坐姿/站姿

**单图直出类模型改不了全身姿态。** 它们从你给的那一帧往下 animate，
头和上半身能动，站着的人不会因为一句提示词就坐下。

`promptlib` 里的「坐/站」这一组只在**会重新生成身体**的模型上有机会生效，
也就是带 `prompt` 参数的那几个：`skyreels-std`、`skyreels-talking`、
`pruna-avatar`、`omnihuman-15`。调试台会在勾选的模型都不支持时给出提示。

**可靠做法是先改照片。** 图像编辑模型很便宜：

| 模型 | base | 说明 |
| :-- | --: | :-- |
| `wavespeed-ai/flux-kontext-dev-ultra-fast` | 0.02 | 最便宜 |
| `bytedance/seedream-v4/edit` | 0.027 | 中文理解好 |
| `alibaba/wan-2.7/image-edit` | 0.03 | |

`promptlib.PHOTO_EDITS` 里备好了五条现成提示词：站→坐、坐→站、改成坐在桌前、
把嘴改成闭合、转成正面平视。每条都写死了「保持长相发型穿着光照不变」，
否则改出来是另一个人。

## 还没测的

- **seedance-2.0 / 2.5**（模力方舟，`/async/videos/generations/multimodal`）：
  按算力单元计价（51 / 77），支持图片+视频+音频组合参考，最长 30 秒有声视频。
  已按 `content[]` + multipart 接入：提示词进数组，本地文件走 `files=`，
  不能写进 JSON body（网关会丢掉，报缺少必填字段 `content`）。
  Vidu / HappyHorse / Wan 挂在 `generations` 上的「图生视频」同样走这套。
- `sync/react-1`（base 0.835）、`veed/fabric-1.0`（0.35）：偏贵，没优先级。
- `kwaivgi/kling-lipsync/*`（0.14~0.15）：可灵的口型替换，和 sync 系列同类。
