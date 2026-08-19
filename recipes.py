#!/usr/bin/env python3
"""方案矩阵：把「用哪个模型、走哪条路线、要什么输入、多少钱」集中定义在一处。

四条路线：

  A 单图直出   一张图 + 音频 → 说话视频。最省事，但长视频容易身份漂移／色偏。
  B 静默素材   一张图 → 10~15 秒「闭嘴微动」视频。阿里云 2D 小样本版就要这个，
               不需要口播，是所有路线里最省钱、最不容易崩的。
  C 角色替换   模板视频（真人拍或生成）+ 图/音频。长视频稳定性最好，因为运动和
               背景来自真实视频，不指望模型自己保持 60 秒一致。
  U 超分收尾   所有路线共用。腾讯云/阿里云都硬性要求 1080P 起，而多数数字人模型
               只输出 480p/720p，所以这一步基本是必选项。

关于计价，有个坑必须说清楚：**base_price 的单位因模型而异**，WaveSpeed 没有
统一。实测例子：

  InfiniteTalk   base_price=0.15，官方描述写「720p tier $0.30/5s」→ 单位是「每 5 秒」
  SkyReels V3    base_price=0.04，模型页价目表写「5s=$0.20」    → 单位是「每 1 秒」

WaveSpeed 自己在文档里也写了「Documentation prices are for reference and may be
outdated. The final task charge prevails」。所以这里的 price_for() 只是**粗估**，
真实成本以 avatar_lab.py 每次调用前后的余额差额为准——跑过的模型会自动写进
data/cost_calibration.json，之后的估算就用实测值，越跑越准。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 分辨率对 base_price 的倍率
RESOLUTION_MULTIPLIER = {"480p": 1.0, "720p": 2.0, "1080p": 4.0, "2k": 4.0, "4k": 8.0}

ROUTE_LABELS = {
    "A": "单图直出",
    "B": "静默素材",
    "C": "角色替换",
    "U": "超分收尾",
}


@dataclass(frozen=True)
class Recipe:
    key: str                       # 命令行短名
    route: str                     # A / B / C / U
    model: str                     # WaveSpeed model_id
    base_price: float              # 单价，含义取决于 price_unit
    needs: tuple[str, ...]         # 必需输入：image / audio / video
    label: str
    resolution: str | None = None  # 提交给模型的 resolution，None 表示该模型没有这个参数
    native_1080p: bool = False     # True 表示原生就能出 1080p，可跳过超分
    params: dict = field(default_factory=dict)   # 固定附加参数
    prompt_required: bool = False
    price_unit: str = "5s"         # "5s" 每 5 秒块 / "s" 每秒 / "run" 每次
    verified_per_second: float | None = None  # 已实测确认的美元/秒，优先于公式
    note: str = ""

    def billable_seconds(self, seconds: float) -> float:
        """路线 B 的时长由模型的 duration 参数写死，与音频长度无关。"""
        return float(self.params.get("duration", seconds))

    def price_for(self, seconds: float, measured_per_second: float | None = None) -> float:
        """粗估成本。有实测单价（本地校准表 > 代码里已验证值）就优先用。"""
        billable = self.billable_seconds(seconds)
        measured_per_second = measured_per_second or self.verified_per_second
        if measured_per_second:
            return round(billable * measured_per_second, 4)

        mult = RESOLUTION_MULTIPLIER.get(self.resolution or "480p", 1.0)
        if self.price_unit == "run":
            return round(self.base_price * mult, 4)
        if self.price_unit == "s":
            return round(billable * self.base_price * mult, 4)
        blocks = max(1, -(-int(round(billable)) // 5))
        return round(blocks * self.base_price * mult, 4)


# --------------------------------------------------------------------------
# 路线 A：单图直出（image + audio → 说话视频）
# --------------------------------------------------------------------------
_ROUTE_A = [
    Recipe("skyreels-std", "A", "skywork-ai/skyreels-v3-standard/single-avatar", 0.04,
           ("image", "audio"), "SkyReels V3 标准版单人",
           prompt_required=True, price_unit="s", verified_per_second=0.04,
           note="已实测：$0.04/秒，60 秒 $2.40。模型页价目表 5s=$0.20 与之吻合"),
    Recipe("skyreels-pro", "A", "skywork-ai/skyreels-v3-pro/single-avatar", 0.08,
           ("image", "audio"), "SkyReels V3 专业版单人",
           prompt_required=True, price_unit="s",
           note="与标准版同族，按秒计价，贵一倍"),
    Recipe("infinitetalk-fast", "A", "wavespeed-ai/infinitetalk-fast", 0.075,
           ("image", "audio"), "InfiniteTalk 快速版",
           note="固定 480p（没有 resolution 参数），长视频一致性是它的强项"),
    Recipe("infinitetalk-480", "A", "wavespeed-ai/infinitetalk", 0.15,
           ("image", "audio"), "InfiniteTalk 480p", resolution="480p",
           note="480×832，比例 1.733 不是标准 9:16，入库前必须补边"),
    Recipe("infinitetalk-720", "A", "wavespeed-ai/infinitetalk", 0.15,
           ("image", "audio"), "InfiniteTalk 720p", resolution="720p",
           note="720×1280 是精确 9:16，超分到 1080p 只需 1.5 倍，画质损失最小"),
    Recipe("omnihuman", "A", "bytedance/avatar-omni-human", 0.12,
           ("image", "audio"), "字节 OmniHuman"),
    Recipe("omnihuman-15", "A", "bytedance/avatar-omni-human-1.5", 0.16,
           ("image", "audio"), "字节 OmniHuman 1.5",
           note="口碑最好的闭源数字人之一，表演自然度通常明显优于开源模型"),
    Recipe("ltx-lipsync", "A", "wavespeed-ai/ltx-2.3/lipsync", 0.1,
           ("image", "audio"), "LTX-2.3 Lipsync 1080p",
           resolution="1080p", native_1080p=True,
           note="唯一原生 1080p 的单图路线，省掉超分环节，入库风险最低"),
    Recipe("hunyuan-avatar", "A", "wavespeed-ai/hunyuan-avatar", 0.15,
           ("image", "audio"), "腾讯混元 Avatar 720p", resolution="720p",
           note="和腾讯数智人同源，理论上入库适配性最好"),
    Recipe("longcat-15", "A", "wavespeed-ai/longcat-avatar-1.5", 0.2,
           ("image", "audio"), "美团 LongCat Avatar 1.5 720p", resolution="720p",
           note="自建要 40GB 显存跑一小时，这里按秒付费直接用"),
    Recipe("multitalk", "A", "wavespeed-ai/multitalk", 0.15,
           ("image", "audio"), "MultiTalk"),
    Recipe("kling-avatar-std", "A", "kwaivgi/kling-v2-ai-avatar-standard", 0.28,
           ("image", "audio"), "可灵 V2 数字人标准版",
           note="你已经在用的可灵，放进来做同条件横向基准"),
    Recipe("kling-avatar-pro", "A", "kwaivgi/kling-v2-ai-avatar-pro", 0.56,
           ("image", "audio"), "可灵 V2 数字人专业版"),
]

# --------------------------------------------------------------------------
# 路线 B：静默素材（image → 闭嘴微动视频）
# 阿里云 2D 小样本视频版只要 10 秒~2 分钟的无口播素材，口型由平台的通用口型模型驱动。
# 官方文档甚至直接给了 AI 生成提示词，说明平台本来就预期你用 AI 生成这段素材。
# --------------------------------------------------------------------------
_ROUTE_B = [
    Recipe("silent-seedance-1080", "B", "bytedance/seedance-v1-pro-i2v-1080p", 0.6,
           ("image",), "Seedance V1 Pro 静默素材 1080p",
           resolution=None, native_1080p=True,
           params={"duration": 10, "aspect_ratio": "9:16", "camera_fixed": True},
           note="原生 1080p + 固定机位 + 可指定 10 秒，最贴合阿里云小样本要求"),
    Recipe("silent-wan-720", "B", "wavespeed-ai/wan-2.2/i2v-5b-720p", 0.05,
           ("image",), "Wan2.2 5B 静默素材 720p",
           params={"duration": 5},
           note="便宜的试错档，验证提示词是否真能压住嘴部动作，再上 1080p"),
    Recipe("silent-wan-480", "B", "wavespeed-ai/wan-2.2/i2v-480p", 0.15,
           ("image",), "Wan2.2 静默素材 480p",
           params={"duration": 8}),
]

# --------------------------------------------------------------------------
# 路线 C：角色替换（模板视频 + 图/音频）
# --------------------------------------------------------------------------
_ROUTE_C = [
    Recipe("animate-replace", "C", "wavespeed-ai/wan-2.2/animate", 0.2,
           ("image", "video"), "Wan2.2-Animate 换人（replace）",
           resolution="720p", params={"mode": "replace"},
           note="把模板视频里的人换成你的角色图，保留原运动/场景/光照。第三方托管标称上限 120 秒"),
    Recipe("animate-animate", "C", "wavespeed-ai/wan-2.2/animate", 0.2,
           ("image", "video"), "Wan2.2-Animate 驱动（animate）",
           resolution="720p", params={"mode": "animate"},
           note="让角色图模仿模板视频的动作，背景重新生成"),
    Recipe("animate-2", "C", "wavespeed-ai/wan-2.2/animate-2", 0.2,
           ("image", "video"), "Wan2.2-Animate 2 代",
           resolution="720p",
           note="schema 里明确写了驱动视频 up to 120 seconds"),
    Recipe("lipsync-2-pro", "C", "sync/lipsync-2-pro", 0.08,
           ("video", "audio"), "Sync Lipsync 2 Pro 换口型",
           params={"sync_mode": "cut_off"},
           note="只改嘴部，身体/背景/光照全来自原视频，结构上不可能漂移"),
    Recipe("lipsync-3", "C", "sync/lipsync-3", 0.135,
           ("video", "audio"), "Sync Lipsync 3 换口型",
           params={"sync_mode": "cut_off"}),
    Recipe("latentsync", "C", "wavespeed-ai/latentsync", 0.05,
           ("video", "audio"), "LatentSync 换口型",
           note="开源方案的托管版，便宜，口型精度不如 sync 系列"),
    Recipe("infinitetalk-v2v", "C", "wavespeed-ai/infinitetalk/video-to-video", 0.15,
           ("video", "audio"), "InfiniteTalk 视频配音 720p", resolution="720p",
           note="拿真人模板视频当输入，官方建议用它规避单图长视频的色偏"),
]

# --------------------------------------------------------------------------
# 路线 U：超分收尾
# --------------------------------------------------------------------------
_ROUTE_U = [
    Recipe("up-bytedance", "U", "bytedance/video-upscaler", 0.0072,
           ("video",), "字节视频超分 → 1080p",
           params={"target_resolution": "1080p"}, verified_per_second=0.0083,
           note="已实测：约 $0.008/秒，60 秒 $0.50。最便宜的 1080P 达标手段"),
    Recipe("up-wavespeed", "U", "wavespeed-ai/video-upscaler", 0.025,
           ("video",), "WaveSpeed 视频超分 → 1080p",
           params={"target_resolution": "1080p"}),
    Recipe("up-seedvr2", "U", "wavespeed-ai/seedvr2/video", 0.1,
           ("video",), "SeedVR2 视频超分 → 1080p",
           params={"target_resolution": "1080p"},
           note="质量档，字节超分出现涂抹感时换它"),
    Recipe("up-ultimate", "U", "wavespeed-ai/ultimate-video-upscaler", 0.15,
           ("video",), "Ultimate 视频超分 → 1080p",
           params={"target_resolution": "1080p"}),
    Recipe("up-bytedance-4k", "U", "bytedance/video-upscaler", 0.0072,
           ("video",), "字节视频超分 → 4K",
           params={"target_resolution": "4k"},
           note="腾讯高精版必须 4K 才收，用这个补齐。4K 单价未实测，估算会偏低"),
]

RECIPES: dict[str, Recipe] = {r.key: r for r in (_ROUTE_A + _ROUTE_B + _ROUTE_C + _ROUTE_U)}


def by_route(route: str) -> list[Recipe]:
    return [r for r in RECIPES.values() if r.route == route.upper()]


def resolve(keys: list[str]) -> list[Recipe]:
    """把命令行传进来的名字解析成 Recipe。支持 `all`、路线名 `A`、以及逗号分隔。"""
    out: list[Recipe] = []
    for raw in keys:
        for key in str(raw).split(","):
            key = key.strip()
            if not key:
                continue
            if key.lower() == "all":
                out.extend(RECIPES.values())
            elif key.upper() in ROUTE_LABELS:
                out.extend(by_route(key))
            elif key in RECIPES:
                out.append(RECIPES[key])
            else:
                raise KeyError(
                    f"未知方案 {key!r}。可选：{', '.join(sorted(RECIPES))}，"
                    f"或路线 {'/'.join(ROUTE_LABELS)}，或 all"
                )
    # 去重但保持顺序
    seen: set[str] = set()
    return [r for r in out if not (r.key in seen or seen.add(r.key))]


def build_payload(recipe: Recipe, *, image_url: str = "", audio_url: str = "",
                  video_url: str = "", prompt: str = "", seed: int | None = None) -> dict:
    """按 recipe 的模型 schema 组装请求体。"""
    payload: dict = dict(recipe.params)

    if "image" in recipe.needs:
        if not image_url:
            raise ValueError(f"{recipe.key} 需要 image")
        payload["image"] = image_url
    if "audio" in recipe.needs:
        if not audio_url:
            raise ValueError(f"{recipe.key} 需要 audio")
        payload["audio"] = audio_url
    if "video" in recipe.needs:
        if not video_url:
            raise ValueError(f"{recipe.key} 需要 video（模板视频）")
        payload["video"] = video_url

    if recipe.resolution:
        payload["resolution"] = recipe.resolution
    if prompt:
        payload["prompt"] = prompt
    elif recipe.prompt_required:
        raise ValueError(f"{recipe.key} 的 prompt 是必填项，用 --prompt/--prompt-file 提供")
    if seed is not None and seed >= 0:
        payload["seed"] = seed

    return payload
