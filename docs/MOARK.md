# 国内通道（模力方舟 / Gitee AI）实测记录

所有结论都是拿真实 token 打接口打出来的。官方文档站是前端渲染，抓不到内容，
所以这份文档就是给后来人省掉重复摸索的。

## Base URL

`https://api.moark.com/v1` 和 `https://ai.gitee.com/v1` **是同一个后端**，
两个都能用（同一 token 在两边拿到完全一样的资源包余额）。代码默认走前者。

## 端点和单价不要写死

`GET /v1/models?include_details=true` 里每个模型都带 `operations`：

```json
{"id": "Duix-Avatar",
 "operations": [{"type": "audio_video2video", "name": "数字人生成",
                 "path": "v1/async/videos/audio-video-to-video",
                 "price": "0.0100", "unit_tag": {"name": "秒"}}]}
```

`moarkclient.video_models()` 就是解析这个。手写映射已经错过一次——把数字人端点
猜成 `image-video-to-video`，实际是 `audio-video-to-video`，白试了好几轮。

**但 price 不一定准**：Duix-Avatar 标 ¥0.01/秒，实测 6 秒扣 ¥0.6 = ¥0.1/秒，
差 10 倍。真实成本看任务返回里的 `price`。

## 数字人相关模型实测

| 模型 | 类型 | 端点 | 标价 | 实测 |
| :-- | :-- | :-- | :-- | :-- |
| **Duix-Avatar** | 口型替换 | `audio-video-to-video` | ¥0.01/秒 | **¥0.6 / 6 秒**，效果最好 |
| LTX-2 | 图生视频（自带音频） | `image-to-video` | ¥0.3/秒 | ¥1.5，2.09s 1280×768 |
| Wan2_2-I2V-A14B | 图生视频 | `image-to-video` | ¥1.5/次 | ¥1.5，720p **15fps 无音轨** |
| Duix.Heygem | 口型替换 | `audio-video-to-video` | ¥0.5/次 | 「该模型已停用」 |
| InfiniteTalk | 图生视频 | `image-to-video` | ¥0.5/次 | 平台侧 Service Temporarily Unavailable |
| seedance-2.0 / 2.5 | 多模态 | `generations/multimodal` | 按算力单元 | 未测 |

### Duix-Avatar 是国内唯一真正能用的音频驱动方案

它是**口型替换**，要 `ref_audio` + `ref_video`（模板视频），不是单张图。
实测 6.03 秒音频出 6.00 秒成片，客观指标是目前测过的所有模型里最好的：

```
缺陷 0 重 / 1 轻
体态    肩线波动 3.9°   肩宽CV 0.008   颈部隆起 1.33（无异常）
一致性  身份相似度 0.921  漂移 -0.020
合理性  人脸检出率 100%
协调性  口型相关性 0.13（偏低，唯一的问题）
```

稳定性来自模板视频而不是生成——这正是路线 C 的价值：**模板拍一次可以反复用，
之后每换一段台词只按秒计费**。对比 WaveSpeed 的 OmniHuman 1.5（$0.156/秒），
Duix-Avatar 约 ¥0.1/秒 ≈ $0.014/秒，**便宜一个数量级**。

代价是要先有一段模板视频，而且口型相关性只有 0.13，需要人眼确认。

## 平台稳定性：约一半的概率失败

同一组参数、同一批文件，实测 **2 次成功 / 3 次失败**。而且时间分布是双峰的：

- 成功的都在 **~20 秒**完成
- 失败的都磨到 **~130 秒**才返回 `Service Temporarily Unavailable`

看起来是后端有健康和故障两组节点在轮询分配。**失败不扣费**（`price` 为 null）。

所以：不要自动重试（要花钱且可能重复扣费），但调试台上给了「用同样的参数重试」
按钮，失败了点一下就行。

## 必须知道的四个坑

1. **文件参数必须走 multipart。** 写在 JSON 里会被网关静默丢掉，
   然后报「必传参数: xxx」，让人以为字段名写错了。
   JSON 模式只认 `image_url` 这种 URL 字段。

2. **状态词是 `failure` 不是 `failed`。** 完整取值：
   `waiting → in_progress → success / failure / cancelled`。
   按 `failed` 判终态会在失败任务上一直轮询到超时。

3. **`output.error` 有两种形状**，直接 `.get("message")` 会 AttributeError：
   ```json
   {"error": {"code": 500, "message": "Service Temporarily Unavailable"}}
   {"error": "An unexpected error has occurred, please check the server log."}
   ```

4. **轮询要重试，提交不能重试。** 实测一次网络读超时就把整条已付费任务判成失败，
   而远端还在跑。但提交类请求读超时后无法判断服务端是否已建任务，
   重试会重复扣费。所以 `_get_with_retry` 只用在 GET 上。

## 字段名对照

清单里没有文件字段名，只能靠报错反推。已确认：

| 端点 | 必填文件 | 可选 |
| :-- | :-- | :-- |
| `image-to-video` | `image`（或 JSON 的 `image_url`） | `cond_video`, `cond_audio`, `audio` |
| `audio-video-to-video` | `ref_audio`, `ref_video` | — |
| `image-video-to-video` | `ref_image`, `drive_video` | — |
| `generations` | 无（纯文生） | — |
| `generations/multimodal` | `content[]` 数组 | — |

`moarkclient.resolve_files()` 负责把「角色图 / 驱动音频 / 模板视频」这三个
语义角色翻译成各端点的实际字段名，调用方不用记。

## 常用命令

```bash
export MOARK_API_KEY=xxx

python3 moarkclient.py known              # 本地已校准的模型和必填字段
python3 moarkclient.py models --filter talk
python3 moarkclient.py quota              # 并发配额
python3 moarkclient.py task <task_id>     # 查任务（含失败原因和扣费）

# 跑一条数字人
python3 moarkclient.py run Duix-Avatar \
    --audio speech.mp3 --video template.mp4 -o out.mp4
```
