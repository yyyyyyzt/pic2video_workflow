# 可灵数字人视频生成（最小实现）

基于**可灵 AI 数字角色 2.0** 官方 API 的单文件实现：上传**一张角色图** + **一段音频（2~60 秒）** + **一句提示词**，直接生成口型、表情、动作俱全的数字人视频。

不再需要自托管 GPU、模型下载、分块拼接——官方接口单次即支持最长 5 分钟内容（音频驱动模式单段音频上限 60 秒，正好覆盖 60 秒测试）。

- 使用指南：[数字角色 2.0 使用指南](https://docs.qingque.cn/d/home/eZQCNHbAH5WUzp1SCYw0uTUcQ?identityId=2MueRKz7Jhc)
- API 文档：[数字人接口](https://klingai.com/document-api/api/video/avatar)

## 快速开始

```bash
# 1) 安装依赖（仅 requests + PyJWT 两个包）
pip install -r requirements.txt

# 2) 配置密钥（可灵控制台 → API Keys 获取 AccessKey/SecretKey）
cp .env.example .env   # 填入 KLING_ACCESS_KEY / KLING_SECRET_KEY

# 3) 一条命令生成 60 秒测试视频
python kling_avatar.py \
    --image face.jpg \
    --audio speech_60s.mp3 \
    --prompt "耐心、温柔地讲解，保持微笑，偶尔用手势辅助说明，动作自然" \
    --mode std \
    --output result.mp4
```

脚本会自动完成：本地文件转 Base64 → 提交任务 → 轮询状态 → 下载成片。60 秒素材通常十几分钟内完成。

## 参数说明

| 参数 | 必填 | 说明 |
| :-- | :-- | :-- |
| `--image` | 是 | 角色图，本地文件或 URL。jpg/jpeg/png，≤10MB，宽高 ≥300px，宽高比 1:2.5~2.5:1 |
| `--audio` | 二选一 | 驱动音频，本地文件或 URL。mp3/wav/m4a/aac，≤5MB，**时长 2~60 秒**（口型跟随音频） |
| `--audio-id` | 二选一 | 可灵 TTS 接口生成的音频 ID（30 天内有效） |
| `--prompt` | 否 | 提示词，描述动作/情绪/镜头，≤2500 字符。不写也能生成，写了表演力更强 |
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

写作技巧：

1. **动词要具体**：「摇头晃脑」「扶了一下眼镜」「双手合十」比「动作自然」更有效；
2. **给情绪曲线**：「先……忽然……最后……」这类时序描述能带出表演层次；
3. **控制镜头**：加「固定镜头」「镜头缓缓推进」可以约束运镜；
4. **中英文都支持**，不超过 2500 字符。

## 底层接口（脚本封装的内容）

```
POST {base}/v1/videos/avatar/image2video      # 创建任务
     body: { image, sound_file | audio_id, prompt?, mode? }
GET  {base}/v1/videos/avatar/image2video/{id} # 查询任务（succeed 后含视频 URL）
```

鉴权为 AccessKey/SecretKey 生成的 JWT（HS256，30 分钟有效期），放在 `Authorization: Bearer <token>` 头中。国内域名 `api-beijing.klingai.com`，海外 `api-singapore.klingai.com`，可通过 `.env` 中 `KLING_API_BASE` 切换。

## 常见问题

- **401 鉴权失败**：确认 AK/SK 正确，且域名与账号所属区域匹配（国内/海外密钥不通用）；
- **音频被拒**：检查时长是否在 2~60 秒之间、文件是否 ≤5MB、格式是否为 mp3/wav/m4a/aac；
- **图片被拒**：检查宽高 ≥300px、宽高比在 1:2.5~2.5:1 之间、是否 ≤10MB；Base64 不能带 `data:` 前缀（脚本已处理）；
- **任务排队久**：高峰期 `submitted/processing` 状态可能持续较长，脚本默认等待 1 小时，可用 `--timeout` 调整。
