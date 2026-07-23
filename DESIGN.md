# 技术路线讨论与决策（重构设计文档）

> 本文档回应"请和我思考讨论后再开展执行"：先给出完整的路线对比与调研结论，
> 再说明本次重构最终选择的架构及理由。所有结论均基于 2026-07 的实际调研
>（官方仓库源码、ComfyUI 原生节点源码、社区节点实现、API 服务商文档）。

---

## 1. 调研结论：SCAIL-2 生态现状

### 1.1 模型本体

- **zai-org/SCAIL-2**（Apache 2.0，2026-06 开源）：基于 Wan 2.1 14B 的端到端角色
  动画模型，无需骨架/深度图等中间表示，原生支持**角色替换（Replacement）**与
  **动作迁移（Animation）**两种模式，动作（含口型、表情）直接从驱动视频的
  latent 迁移，因此**口型同步是模型自带能力**，不需要额外的对口型阶段。
- **模型的硬约束**：单次生成窗口 **81 帧**；分辨率 512p/704p 档，宽高需被 32 整除；
  latent 时间压缩 4:1，因此帧数必须满足 **4n+1**。
- **关键发现（决定架构的核心事实）**：SCAIL-2 **训练时就是按"81 帧窗口 + 5 帧
  重叠（步进 76）"训练的**。ComfyUI 原生节点 `WanSCAILToVideo` 暴露了
  `previous_frames` / `previous_frame_count` / `video_frame_offset` 三个扩展输入：
  把上一段**已解码的输出帧**传入，节点会将其末尾 5 帧 VAE 编码后写入新段 latent
  的头部，并用 `noise_mask` 冻结这些 latent（不加噪、不重采样），模型在
  "已知前 5 帧"的条件下续写后 76 帧。**这是模型级的语义锚定**，身份、服装、
  光影、动作速度全部由模型上下文保证——正是"禁止伪造拼接"约束所要求的机制。

### 1.2 官方推理仓库 vs ComfyUI 原生节点

| 能力 | 官方 `generate.py` | ComfyUI 原生节点 |
| :-- | :-- | :-- |
| 单段推理 | ✅ | ✅ |
| `previous_frames` 长视频锚定 | ❌（无此接口） | ✅（`WanSCAILToVideo`） |
| SAM3 掩码预处理 | 需单独跑 SCAIL-Pose 子模块（MMPose 环境） | ✅ 内置 `SAM3_VideoTrack` / `SAM3_Detect` / `SCAIL2ColoredMask` |
| 蒸馏加速（lightx2v 6~8 步） | ✅ | ✅ |
| 显存管理 | 手动 | 自动 offload + `/free` 接口 |

结论：**自托管走 ComfyUI 是唯一同时具备"长视频锚定 + 一体化预处理"的路径**，
官方仓库反而不适合直接集成（无锚定接口，预处理还要单独装 MMPose 环境）。

### 1.3 社区长视频节点（你提到的两个项目，均已核实存在）

- `collbroGTR/comfyui-scail2-infinity`：单节点内部循环 "chunk → sample → decode →
  re-anchor on last 5 frames → repeat"，固定 81 帧窗口、76 步进。
- `Brobert-in-aus/scail-auto-extend`：同机制，额外做 **Reinhard-LAB 颜色匹配**
  （每段向上一段末帧对齐，抑制颜色漂移），并自动规划尾段长度（4n+1 对齐）。

两者的底层逻辑一致，即本次重构 `LongVideoProcessor` 采用的算法：
**分块（81 帧）→ 前缀锚定（previous_frames，5 帧）→ 颜色校正（Reinhard-LAB）→
重叠区余弦加权融合**。我们不直接依赖这两个自定义节点（避免第三方节点的
维护风险），而是在编排层用**原生节点**复现同样的循环——好处是分块循环在
我们自己的 Python 进程里，可以做断点续传、进度回调、逐块显存清理和失败重试，
这些在"单节点内部 for 循环"的方案里都做不到。

### 1.4 API 服务商（托管推理）

- **fal.ai** `fal-ai/scail-2` 与 **WaveSpeedAI** `wavespeed-ai/scail-2` 均已上线，
  输入为 `image_url + video_url + mode(animation/replacement)`。
- **关键限制：两家都不暴露 `previous_frames` 输入**，即无法做跨段模型级锚定。
  对 1~2 分钟长视频，只能各段独立生成再拼接——身份漂移无法根治，正是你现有
  实现（RunningHub 远程工作流 + 固定 seed + crossfade）遇到的问题，也违反
  "禁止伪造拼接"的硬性约束。
- 结论：**托管 API 只适合 ≤81 帧短片的快速验证**，不能作为长视频主引擎。
  本项目将其保留为可选引擎（`fal`），仅在整段视频可一次提交时使用。

---

## 2. 路线对比与决策

| 路线 | 集成速度 | 长视频语义一致性 | 可控性 | 结论 |
| :-- | :-- | :-- | :-- | :-- |
| A. 自托管 ComfyUI + 原生节点 + 自研编排层 | 中（需 GPU + 下载 ~40GB 权重） | ✅ 模型级锚定 | ✅ 完全开源可控 | **主路线** |
| B. 官方 zai-org/SCAIL-2 推理代码 | 慢（MMPose 预处理环境 + 无锚定接口需改模型代码） | ❌ 需自研 latent 锚定 | ✅ | 放弃 |
| C. fal.ai / WaveSpeed 托管 API | 最快 | ❌ 无跨段锚定 | ⚠️ 黑盒 | 保留为短片引擎 |
| D. 现有 RunningHub 远程工作流（旧实现） | 已有 | ❌ 独立分段 + crossfade（伪造拼接） | ⚠️ 工作流固定 | 标记为 legacy |

**决策：路线 A 为核心**，通过引擎抽象层（`scailswap/engines/`）同时保留 C，
未来若服务商暴露锚定接口可平滑切换。旧实现（`roleswap/` + Flask `web/`）
整体保留在仓库中标记为 legacy，不再演进。

### 为什么"模型级锚定 + 轻量融合"两者都要？

- `previous_frames` 锚定解决的是**语义一致性**：身份、服装、动作连续性由模型
  在生成时保证，新段的前 5 帧 latent 直接复用上一段的输出（冻结不重采样）。
- 但 VAE 编解码往返 + 采样噪声仍会造成**极轻微的像素级差异与低频颜色漂移**
  （逐段累积后可见）。因此在拼接层再做两件事：
  1. **Reinhard-LAB 颜色匹配**：把新段整体颜色统计对齐到上一段末帧，阻断漂移累积；
  2. **重叠区余弦（Hann）/高斯渐变融合**：5 帧重叠区做像素级加权过渡，消除残余接缝。
- 这不是"用融合代替语义一致性"，而是"模型保证语义、融合抹平数值残差"，
  与 `scail-auto-extend` 的做法一致。

### 口型同步

驱动视频中的口型属于面部动作，SCAIL-2 端到端迁移时一并处理，且锚定机制保证
跨段连续；最终输出直接合回**原始音轨**（帧数与源视频 1:1、帧率一致，音画天然
对齐）。若对口型精度仍不满意，提供 **Wav2Lip 后处理开关**（`enable_wav2lip`），
在最终成片上以原始音频再对齐一次口型，音频流全程未被切割，严格对齐。

---

## 3. 长视频算法（`LongVideoProcessor` 核心逻辑）

```
源视频（任意帧率/时长）
   │ ffprobe 探测 fps / 总帧数（输出严格保持同帧率、同帧数）
   ▼
分块规划 ChunkPlanner
   窗口 81 帧、重叠 5 帧、步进 76（模型训练配置，可调）
   chunk_i 覆盖源帧 [76·i, 76·i + 81)；尾段不足时补齐到 4n+1（复制末帧），生成后裁回
   ▼
逐块串行生成（锚定链决定必须串行）
   chunk_0：正常生成
   chunk_i (i>0)：把 chunk_{i-1} 的完整输出作为 previous_frames 传入，
                  模型取其末尾 5 帧 VAE 编码后冻结为新段 latent 头部 → 续写 76 新帧
   每块完成后：Reinhard-LAB 颜色匹配 → 落盘（断点续传状态）→ 引擎清显存(/free)
   OOM：清显存(aggressive) → 指数退避重试
   ▼
流式拼接（不整段驻留内存）
   重叠区 5 帧：余弦/高斯渐变权重逐像素融合（前段淡出、后段淡入）
   ▼
按源帧率写出无声视频 → 裁至源总帧数 → 合回原始音轨 → （可选）Wav2Lip 精修
```

关键实现文件：

- `scailswap/chunking.py` —— 分块规划（81/76/4n+1 数学）
- `scailswap/blending.py` —— 余弦/高斯权重 + Reinhard-LAB 颜色迁移
- `scailswap/engines/comfyui_engine.py` —— 用原生节点逐块构图提交
  （`WanSCAILToVideo.previous_frames` 锚定链），OOM 检测、`/free` 显存清理
- `scailswap/processor.py` —— `LongVideoProcessor`：调度、重试、断点续传、
  进度回调、流式拼接、音频合并

## 4. 遗留问题 / 后续可选优化

1. **多人替换**：`SCAIL2ColoredMask` 支持多身份配色（最多 6 个），当前 API 暴露了
   `object_indices` / `max_objects` 参数，多参考图（multi-reference）暂未封装。
2. **超长视频（>3 分钟）**：锚定链逐段传递，理论无上限，但建议每 ~90 段（约 5 分钟）
   插入一次"参考图重锚"防漂移，目前未实现（1~2 分钟场景不需要）。
3. **fal / WaveSpeed 若未来暴露 previous_frames**：只需在对应引擎里填上 anchor
   字段即可获得与自托管一致的长视频能力。
