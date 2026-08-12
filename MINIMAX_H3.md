# 用 MiniMax H3 做 70 秒视频：可行性研究与方案

> 结论基于 2026-08 的官方文档与开源模型卡实测核对。
> 工具实现：[`minimax_h3.py`](minimax_h3.py)；分镜示例：[`prompts/h3_storyboard_example.txt`](prompts/h3_storyboard_example.txt)

## 一句话结论

**画面可以串到 70 秒，但"70 秒口播成片"这件事上，H3 单靠首尾帧串接做不出可交付的结果——瓶颈不在画面，在音频。**

如果只是要"70 秒的好看画面"，H3 串接完全可行，本工具已实现。
如果要的是"人物念完这段 70 秒台词、口型对得上、音色全程一致"，需要额外一道对口型工序，
或者继续用可灵数字人做口播主体、H3 只做画面点缀。

---

## 一、H3 的硬性规格（核对过官方文档）

| 项目 | 规格 |
| :-- | :-- |
| 单次时长 | **4~15 秒，整数** |
| 分辨率 / 帧率 | 768P / 2K，**24 FPS** |
| 音频 | **模型自己生成** 32kHz 立体声 |
| 首尾帧模式（fl2va） | 0/1/2 张图：`first_frame`、`last_frame` |
| 参考模式（ref2va） | 图 ≤9 张、视频 ≤3 段、音频 ≤3 段（**视频与音频总时长各 ≤15 秒**） |
| 素材传入 | 公网 URL / `mm_file://{file_id}` / **`data:image/xxx;base64,` data URI**；请求体 ≤64MB |
| 提示词 | ≤7000 字符 |
| 价格 | 768P **0.50 元/秒**；2K **0.80 元/秒**；768P→2K 再生成 0.30 元/秒 |
| 输入计费 | 音频免费；图片 5 张内免费，超出 0.20 元/张；**参考视频按秒计费**（同输出单价） |

### 两个决定性约束

**1. `first_frame`/`last_frame` 与 `reference_*` 互斥**

> content 中出现 `reference_image` / `reference_video` / `reference_audio` 任一 role，
> 就不能再出现 `first_frame` / `last_frame`（反之亦然），二者不可混用。

这直接砍掉了最理想的方案——"用上一段末帧接首帧保证连续 + 同时用角色图锚定身份防漂移"。
只能二选一，于是有了下面两条链。

**2. H3 是"生成音频"，不是"音频驱动口型"**

可灵数字人：你给音频 → 它对口型（音画天然对齐、台词精确、音色是你的）。
H3：你给文字 → 它同时生成画面和配音（音色由模型定、台词由模型念）。

`reference_audio` 可以给音色参考，但**总时长上限 15 秒**，喂不进 70 秒的完整配音。

---

## 二、四种方案与取舍

### 方案 A：frame 链（画面连贯优先）★ 已实现

段 i 的 `first_frame` = 段 i-1 的末帧。

```bash
python3 minimax_h3.py chain --storyboard prompts/h3_storyboard_example.txt \
    --mode frame --image face.jpg --resolution 768P -o h3_long.mp4
```

- **优点**：衔接处画面天然连续（下一段的第一帧就是上一段的最后一帧），几乎看不到跳变
- **缺点**：因为互斥约束，**无法再用参考图锚定身份**。误差会沿链累积——第 5 段的人可能已经和原图不太像了
- 适合：镜头连续推进的叙事、运镜连贯的产品展示

### 方案 B：reference 链（身份一致优先）★ 已实现

每段都传同一批 `reference_image`（角色图，可选加上一段末帧）。

```bash
python3 minimax_h3.py chain --storyboard xxx.txt --mode reference \
    --image face.jpg --carry-frame --transition 0.4 -o h3_long.mp4
```

- **优点**：身份锚点始终是原图，**不累积漂移**；`--carry-frame` 把上一段末帧也塞进参考图兼顾连贯
- **缺点**：段边界画面会跳变，需要 `--transition 0.3~0.5` 用交叉溶解掩盖（会吃掉总时长）
- 适合：分镜切换本来就存在的场景（每段是不同机位/景别）

### 方案 C：H3 出画面 + 自己的配音 + 对口型（口播唯一可交付路径）

```bash
# 1) H3 只出画面，直接把它生成的音频换成你的配音
python3 minimax_h3.py chain --storyboard xxx.txt --mode frame --image face.jpg \
    --audio replace --external-audio my_voice.mp3 -o h3_visual.mp4

# 2) 再走一道对口型（可灵 advanced-lip-sync 支持 ≤60s，或本地 wav2lip）
```

换完音轨后，画面里的口型仍然对应 H3 自己念的内容，**必须再对一次口型**，否则口型和你的台词对不上。
这是把 H3 的画面力用在口播上的唯一正路，代价是多一道工序、多一次质量损失。

### 方案 D：混合（对你当前项目最实用）

- **可灵数字人**做口播主体：音画天然对齐、支持长视频、一次成片
- **H3** 做 B-roll / 空镜 / 产品特写 / 开场结尾包装：15 秒内它的画面表现力和运镜明显更强
- 剪辑台拼起来

口播用可灵、画面用 H3，各用各的长处，不用跟任何一边的限制硬碰。

---

## 三、成本（70 秒）

| 方案 | 算式 | 成本 |
| :-- | :-- | --: |
| 768P，5 段 × 14s | 70 × 0.50 | **35 元** |
| 2K 直出 | 70 × 0.80 | **56 元** |
| 768P 生成后再生成 2K | 70×0.50 + 70×0.30 | 56 元 |
| reference 链传参考视频（不推荐） | 上面 + 每段输入视频按秒计费 | 可能翻倍 |

`plan` 子命令会直接算给你看，不花钱：

```bash
python3 minimax_h3.py plan --total 70
python3 minimax_h3.py plan --storyboard prompts/h3_storyboard_example.txt
```

**参考视频要按秒计费**这一点容易踩坑：把上一段 14 秒视频当 `reference_video` 传进去，
每段就多花 7 元（768P），5 段多 35 元、直接翻倍。所以本工具的 reference 链默认只传**图片**
（末帧图），不传视频。

---

## 四、测试命令（按成本从低到高）

**第 0 步：配置密钥（免费）**

```bash
# .env 里加一行，密钥在 https://platform.minimaxi.com/user-center/basic-information/interface-key
MINIMAX_API_KEY=your_key
```

**第 1 步：规划与成本估算（0 元）**

```bash
python3 minimax_h3.py plan --total 70
python3 minimax_h3.py plan --storyboard prompts/h3_storyboard_example.txt
```

**第 2 步：dry-run 预演，看清每段要发什么（0 元）**

```bash
python3 minimax_h3.py chain --storyboard prompts/h3_storyboard_example.txt \
    --image face.jpg --mode frame --dry-run

python3 minimax_h3.py chain --storyboard prompts/h3_storyboard_example.txt \
    --image face.jpg --mode reference --carry-frame --transition 0.4 --dry-run
```

**第 3 步：单段连通性验证（2 元）**

```bash
python3 minimax_h3.py single --duration 4 --resolution 768P \
    --prompt "固定机位中景，白衬衫藏青马甲的女讲解员站在纯白影棚里，双手交握身前，浅笑看镜头。角色说话：「大家好」" \
    -o h3_probe.mp4
```

先确认密钥、余额、参数都对，再往下花钱。

**第 4 步：两段小样，验证衔接效果（约 4 元）**

这一步最关键——**决定走 frame 还是 reference，别直接跑全长**。

```bash
cat > /tmp/sb2.txt <<'EOF'
## 4s | 第一段
固定机位中景，纯白影棚，白衬衫藏青马甲蓝白领结盘发的女讲解员，双手交握身前，浅笑看镜头。
角色说话：「各位朋友大家好」

## 4s | 第二段
固定机位中景，纯白影棚，同一位白衬衫藏青马甲蓝白领结盘发的女讲解员，站姿延续上一段，双手仍交握身前。
角色说话：「今天聊家庭安全用电」
EOF

# A：frame 链
python3 minimax_h3.py chain --storyboard /tmp/sb2.txt --mode frame \
    --image face.jpg --workdir data/h3_try_frame -o try_frame.mp4

# B：reference 链
python3 minimax_h3.py chain --storyboard /tmp/sb2.txt --mode reference \
    --image face.jpg --carry-frame --transition 0.4 \
    --workdir data/h3_try_ref -o try_ref.mp4
```

看两件事：**衔接处跳不跳**、**第二段的人还像不像原图**。

**第 5 步：全长 70 秒（35 元）**

```bash
python3 minimax_h3.py chain --storyboard prompts/h3_storyboard_example.txt \
    --image face.jpg --mode frame --resolution 768P \
    --workdir data/h3_full -o h3_70s.mp4
```

中断了直接重跑同一条命令，已完成的段会跳过（状态在 `workdir/state.json`）。

**第 6 步：口播场景换掉 H3 的配音**

```bash
python3 minimax_h3.py chain --storyboard prompts/h3_storyboard_example.txt \
    --image face.jpg --mode frame --audio replace --external-audio my_voice.mp3 \
    --workdir data/h3_full -o h3_70s_dub.mp4
```

换完音轨记得**再做一次对口型**，否则口型对应的还是 H3 自己念的内容。

**顺带：抖动后处理也能用在 H3 成片上**

```bash
python3 stabilize.py h3_70s.mp4 --method analyze
```

---

## 五、写分镜提示词的要点（和可灵不同）

可灵数字人的提示词只写"怎么演"，台词交给音频。**H3 相反：台词要写进提示词**，因为声音是它生成的。

1. **台词直接写进 prompt**：`角色说话：「……」`。中文口播约每秒 4~5 字，14 秒段落写 55~70 字，写多了念不完
2. **每段都要重复人物外观与场景**，否则段间会漂（这是分段生成的固有代价）
3. **每段都写"固定机位"**，H3 默认倾向加运镜
4. **让相邻段能接上**：上一段结尾的姿态 = 下一段开头的姿态，前后都写清楚
5. 官方还提供 `H3-Context-IR` 接口帮你把粗提示词扩写成结构化表达（按 token 计费），本工具暂未接入

---

## 六、关于"开源"能不能绕过 15 秒限制

权重确实开放了（H3-Base 33B dense Transformer，BF16，可微调），但**自托管绕不过 15 秒**：

- 模型在 ≤15 秒的序列上训练，强行外推会崩
- `H3-Context-IR`（提示词理解/编排模块）**未开源**，是托管服务
- 首批开源只给了 full attention 推理，稀疏注意力实现还没放出，长序列成本很高
- 33B dense + 视频 latent，自托管显存与工程量都不小

所以"想要更长就自己部署改长度"这条路，短期内不现实。分段串接仍然是唯一实用解。

---

## 七、和可灵数字人的定位对比

| | 可灵数字人 2.0 | MiniMax H3 |
| :-- | :-- | :-- |
| 时长 | 一次成片，支持长视频（实测 108s 可行） | **单次 ≤15 秒**，长视频靠串接 |
| 音频 | **你给音频，它对口型** | **它生成音频**，台词写在提示词里 |
| 台词精确度 | 完全可控（就是你的音频） | 模型念，可能漏字/改字/念不完 |
| 音色一致性 | 全程一致（同一条音轨） | 分段独立生成，**段间可能漂移** |
| 音画对齐 | 天然对齐 | 段内对齐，段间拼接需处理 |
| 画面表现力 | 口播场景够用，动作幅度保守 | **明显更强**，运镜、光影、叙事感好 |
| 身份一致性 | 单次成片，天然稳定 | 分段，需要靠 frame/reference 链维持 |
| 70 秒成本 | 按秒计费（std 较低） | 35 元（768P）/ 56 元（2K） |

**结论**：口播定稿继续用可灵；H3 用来做画面强度高的短段落，或走方案 C 加一道对口型。
