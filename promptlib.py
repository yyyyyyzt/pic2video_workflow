#!/usr/bin/env python3
"""提示词积木。

为什么要拆成积木而不是一堆整段模板：实测反馈是「手型太单一，只有摊手」。
整段模板里手势只是一句话，运营改的时候要在一大段里找那一句，很容易改坏别的约束。
拆开之后，机位和画质那些**不能动**的硬约束固定住，手势、表情、体态各自独立替换，
调试就变成了「只换一个变量」，也才有可比性。

分组：
    BASE        机位、正视镜头、画面干净。对应腾讯录制指引的硬要求，不建议改
    GESTURE     手势。这是最该多试的一组
    EXPRESSION  表情与情绪
    POSTURE     体态与身体幅度
    SPEECH      开口方式、静默段怎么处理
    NEGATIVE    明确禁止项

组合规则：一次各选一个，拼成完整提示词。PRESETS 里是几组已经搭好的。
"""

from __future__ import annotations

from dataclasses import dataclass

# 硬约束，对应腾讯《形象录制指引》。运营不用动这一段。
BASE = (
    "固定三脚架机位，镜头完全静止，无推拉摇移，无变焦。"
    "人物正面朝向镜头，眼睛始终正视镜头，不斜视、不看向别处。"
    "面部全程完整可见、无任何遮挡。"
    "光线均匀柔和，人物与背景亮度稳定，背景整洁静止、无移动物体、无其他人出镜。"
    "画面干净，不要出现任何字幕、水印或文字。"
)


@dataclass(frozen=True)
class Block:
    key: str
    label: str
    text: str
    note: str = ""


# --------------------------------------------------------------------------
# 手势。实测「只有摊手」，所以这一组给足选项。
# 每条都保证不违反「手不遮挡面部颈部」这一硬要求。
# --------------------------------------------------------------------------
GESTURE: tuple[Block, ...] = (
    Block("open-palm", "摊手（默认基线）",
          "双手自然放在身前，讲到重点时有克制的小幅摊手动作，手不抬到肩部以上。",
          "之前一直在用的那种。作为对照基线保留"),
    Block("clasped", "十指交叠，几乎不动",
          "双手在身前自然交叠握在一起，全程基本保持不动，只有极小幅度的呼吸起伏，"
          "不做任何手势。",
          "最保险。手不动就不会画坏，适合优先保证画面稳定的场合"),
    Block("counting", "数点式（第一、第二）",
          "讲到分条内容时用手指自然点数，先竖起一指，再两指、三指，"
          "动作幅度小、速度慢，数完自然放回身前。",
          "台词里有「第一、第二、第三」时用这个，动作和内容对得上"),
    Block("precision", "捏合强调",
          "讲到关键数字或名词时，拇指与食指轻轻捏合作强调，"
          "其余手指自然弯曲，手停在胸前高度。",
          "偏专业、偏讲解的调性"),
    Block("alternating", "单手交替小幅摆动",
          "以单手为主做小幅摆动，左右手交替使用，另一只手自然垂在身侧，"
          "不同时抬起双手。",
          "比双手摊开自然，也不容易糊成一团"),
    Block("down", "双手垂放身侧",
          "双手自然垂放在身体两侧，不做手势，仅随说话有极轻微的自然晃动。",
          "最接近真人播报的站姿，画面最干净"),
    Block("one-hand-rest", "一手扶另一手腕",
          "一只手轻扶另一只手的手腕，置于腹部前方，全程保持这个姿态，"
          "偶尔有极小幅度的松紧变化。",
          "有依托的姿态，比悬空的手更不容易变形"),
    Block("emphasis-fist", "轻握拳下压强调",
          "讲到需要强调的地方，手轻握成拳，向下做一次小幅度下压动作，"
          "随后松开自然放回身前。",
          "语气坚定的段落用"),
)

# --------------------------------------------------------------------------
EXPRESSION: tuple[Block, ...] = (
    Block("warm", "自然亲和",
          "表情自然亲和，随内容有细微变化，不做夸张表情。"),
    Block("calm-pro", "沉稳专业",
          "表情克制专业，眉眼放松，嘴角平和，不刻意微笑，"
          "随内容只有极细微的情绪变化。"),
    Block("earnest", "认真关切",
          "表情认真、略带关切，讲到风险和后果时眉头有轻微收紧，"
          "讲到建议时恢复平和。"),
    Block("bright", "轻松明快",
          "表情轻松明快，眼里有神，讲到正面内容时嘴角自然上扬，"
          "但不咧嘴大笑。"),
)

# --------------------------------------------------------------------------
# 坐/站。运营常遇到「用户传了站姿照片但要坐着说」或者反过来。
#
# 先说清楚能做到什么程度：**单图直出类模型改不了全身姿态**。它们是从你给的那一帧
# 往下animate，头和上半身能动，但站着的人不会因为一句提示词就坐下来。
# 这组提示词只在会重新生成身体的模型上有效（SkyReels、OmniHuman、
# skyreels-v3/talking-avatar 这些带 prompt 参数的），而且成功率不高。
#
# 真要换姿态，可靠做法是**先把照片改了再送进来**——用 photo-edit 组的提示词
# 配 bytedance/seedream-v4/edit 之类的图像编辑模型（约 $0.027 一张），
# 拿到坐姿照片再生成。见 BODY_MODE_WARNING。
# --------------------------------------------------------------------------
BODY_MODE_WARNING = (
    "单图直出类模型改不了全身姿态：站着的人不会因为提示词就坐下。"
    "这组只在带 prompt 参数、会重新生成身体的模型上有一定概率生效。"
    "要稳定换姿态，请先用「改照片姿态」把参考图改成目标姿态。"
)

BODY: tuple[Block, ...] = (
    Block("keep", "跟照片一致（推荐）",
          "保持参考图中的姿态和取景，不改变人物是坐着还是站着。",
          "默认。改姿态不可靠，能不改就不改"),
    Block("to-sitting", "改成坐姿",
          "人物坐在椅子上，上身端正，双肩放平，腰背自然挺直，"
          "取景为坐姿的胸部以上半身景。",
          "照片是站姿、需要坐着说时用。成功率取决于模型"),
    Block("to-standing", "改成站姿",
          "人物自然站立，双脚与肩同宽，重心平稳，上身端正，"
          "取景为站姿的腰部以上半身景。",
          "照片是坐姿、需要站着说时用"),
    Block("sitting-desk", "坐在桌前",
          "人物坐在桌子后方，双手可自然搭在桌面边缘，上身端正面向镜头，"
          "取景为桌面以上的半身景。",
          "访谈、播报台的调性"),
    Block("sitting-sofa", "坐在沙发上",
          "人物坐在沙发上，姿态放松但上身端正，肩线保持水平，"
          "取景为胸部以上半身景。",
          "轻松、口播带货的调性"),
)

POSTURE: tuple[Block, ...] = (
    Block("still", "躯干几乎不动",
          "躯干保持稳定，只有随呼吸的极小起伏，肩线保持水平，无左右摇晃、无前后移动。",
          "体态指标最容易过的一组"),
    Block("micro-nod", "小幅点头",
          "躯干保持稳定，头部只有随语气的小幅点头，不做大幅转头或偏头，"
          "动作缓慢平滑，无突然位移。"),
    Block("slight-lean", "偶尔轻微前倾",
          "躯干基本稳定，讲到重点时上身有极轻微的前倾，随后回正，"
          "肩线始终保持水平。"),
)

SPEECH: tuple[Block, ...] = (
    Block("direct", "开口前闭嘴，直接出声",
          "开口前嘴唇自然闭合，用鼻子安静换气，第一句从闭嘴状态直接开口，"
          "中间不要先张嘴再出声。",
          "默认。防止模型演「深吸一口气」"),
    Block("plain", "不额外约束开口方式",
          "",
          "对照组：看模型自己会怎么处理开头"),
)

NEGATIVE: tuple[Block, ...] = (
    Block("standard", "标准禁止项",
          "不做夸张表情，不舔嘴、不吐舌、不噘嘴。不要深吸气、张嘴预备、耸肩提气。"
          "手不抬到肩部以上，不遮挡面部和颈部。不做快速动作。"),
    Block("plus-neck", "标准 + 颈部",
          "不做夸张表情，不舔嘴、不吐舌、不噘嘴。不要深吸气、张嘴预备、耸肩提气。"
          "手不抬到肩部以上，不遮挡面部和颈部。不做快速动作。"
          "颈部皮肤自然平滑，不要突出锁骨、颈筋或颈部骨骼结构。",
          "实测有模型会把颈部骨骼画得很显眼，这一条针对它"),
    Block("none", "不加禁止项", "", "对照组"),
)

GROUPS: dict[str, tuple[Block, ...]] = {
    "body": BODY,
    "gesture": GESTURE,
    "expression": EXPRESSION,
    "posture": POSTURE,
    "speech": SPEECH,
    "negative": NEGATIVE,
}

GROUP_LABELS = {
    "body": "坐/站",
    "gesture": "手势",
    "expression": "表情",
    "posture": "体态",
    "speech": "开口方式",
    "negative": "禁止项",
}

GROUP_HINTS = {
    "body": BODY_MODE_WARNING,
}

# 改照片姿态用的提示词。配图像编辑模型（约 $0.027 一张）先把参考图改成目标姿态，
# 再拿改好的图去生成——比指望口播模型自己把人从站着变成坐着靠得住得多。
PHOTO_EDITS: dict[str, dict] = {
    "to-sitting": {
        "label": "站姿照 → 坐姿",
        "prompt": "把画面中的人物改为坐在椅子上，保持同一个人的长相、发型、"
                  "穿着和光照完全不变，只改变姿态和取景。上身端正，双肩放平，"
                  "正面朝向镜头，构图为胸部以上半身景，背景保持原样。",
    },
    "to-standing": {
        "label": "坐姿照 → 站姿",
        "prompt": "把画面中的人物改为自然站立，保持同一个人的长相、发型、"
                  "穿着和光照完全不变，只改变姿态和取景。双脚与肩同宽，上身端正，"
                  "正面朝向镜头，构图为腰部以上半身景，背景保持原样。",
    },
    "to-desk": {
        "label": "改成坐在桌前",
        "prompt": "把画面中的人物改为坐在桌子后方，双手自然搭在桌面上，"
                  "保持同一个人的长相、发型、穿着不变，正面朝向镜头，"
                  "构图为桌面以上的半身景。",
    },
    "close-mouth": {
        "label": "把嘴改成闭合",
        "prompt": "把画面中人物的嘴改为自然闭合状态，牙齿不外露，"
                  "其余一切（长相、发型、穿着、姿态、背景、光照）保持完全不变。",
    },
    "frontal": {
        "label": "转成正面平视",
        "prompt": "把画面中的人物改为正面朝向镜头、眼睛平视镜头，"
                  "保持同一个人的长相、发型、穿着和光照不变，构图为半身景。",
    },
}

# 已经搭好的组合。名字对应它想验证的东西。
PRESETS: dict[str, dict] = {
    "baseline": {
        "label": "基线（当前在用的）",
        "note": "摊手 + 亲和 + 小幅点头。作为对照，不要删",
        "blocks": {"body": "keep", "gesture": "open-palm", "expression": "warm",
                   "posture": "micro-nod", "speech": "direct",
                   "negative": "standard"},
    },
    "still-hands": {
        "label": "手不动（最保险）",
        "note": "手交叠不动 + 躯干几乎不动。画面最稳，但可能偏僵",
        "blocks": {"body": "keep", "gesture": "clasped", "expression": "calm-pro",
                   "posture": "still", "speech": "direct",
                   "negative": "plus-neck"},
    },
    "counting": {
        "label": "数点讲解",
        "note": "台词里有「第一第二第三」时用，手势和内容对得上",
        "blocks": {"body": "keep", "gesture": "counting", "expression": "earnest",
                   "posture": "micro-nod", "speech": "direct",
                   "negative": "plus-neck"},
    },
    "single-hand": {
        "label": "单手交替",
        "note": "比双手摊开自然，手也不容易糊",
        "blocks": {"body": "keep", "gesture": "alternating", "expression": "warm",
                   "posture": "micro-nod", "speech": "direct",
                   "negative": "plus-neck"},
    },
    "broadcast": {
        "label": "播报站姿",
        "note": "双手垂放，最接近真人播报，画面最干净",
        "blocks": {"body": "keep", "gesture": "down", "expression": "calm-pro",
                   "posture": "still", "speech": "direct",
                   "negative": "plus-neck"},
    },
    "emphatic": {
        "label": "语气坚定",
        "note": "握拳下压强调 + 轻微前倾",
        "blocks": {"body": "keep", "gesture": "emphasis-fist", "expression": "earnest",
                   "posture": "slight-lean", "speech": "direct",
                   "negative": "plus-neck"},
    },
}


def block(group: str, key: str) -> Block:
    if group not in GROUPS:
        raise KeyError(f"未知分组 {group!r}，可选：{', '.join(GROUPS)}")
    for item in GROUPS[group]:
        if item.key == key:
            return item
    raise KeyError(f"分组 {group} 里没有 {key!r}，"
                   f"可选：{', '.join(b.key for b in GROUPS[group])}")


def compose(blocks: dict[str, str] | None = None, *, base: str = BASE,
            extra: str = "") -> str:
    """把选中的积木拼成一段提示词。

    顺序固定：机位硬约束 → 坐站 → 体态 → 手势 → 表情 → 开口方式 → 禁止项 → 补充。
    固定顺序是为了让两次生成的差异只来自内容而不是语序。
    """
    parts = [base.strip()]
    for group in ("body", "posture", "gesture", "expression", "speech", "negative"):
        key = (blocks or {}).get(group)
        if not key:
            continue
        text = block(group, key).text.strip()
        if text:
            parts.append(text)
    if extra.strip():
        parts.append(extra.strip())
    return " ".join(p for p in parts if p)


def preset(name: str) -> str:
    if name not in PRESETS:
        raise KeyError(f"未知预设 {name!r}，可选：{', '.join(PRESETS)}")
    return compose(PRESETS[name]["blocks"])


def catalog() -> dict:
    """给 Web 页面用的完整目录，可直接 JSON 序列化。"""
    return {
        "base": BASE,
        "groups": {
            group: {
                "label": GROUP_LABELS[group],
                "hint": GROUP_HINTS.get(group, ""),
                "options": [{"key": b.key, "label": b.label,
                             "text": b.text, "note": b.note}
                            for b in items],
            }
            for group, items in GROUPS.items()
        },
        "photo_edits": {k: dict(v) for k, v in PHOTO_EDITS.items()},
        "presets": {
            name: {"label": spec["label"], "note": spec["note"],
                   "blocks": spec["blocks"], "text": compose(spec["blocks"])}
            for name, spec in PRESETS.items()
        },
    }


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="提示词积木：列出选项、拼装预设")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有积木")
    p = sub.add_parser("show", help="打印某个预设拼出来的完整提示词")
    p.add_argument("preset", choices=list(PRESETS))

    args = parser.parse_args()
    if args.cmd == "list":
        for group, items in GROUPS.items():
            print(f"\n[{GROUP_LABELS[group]}] {group}")
            for b in items:
                print(f"  {b.key:<16}{b.label}"
                      + (f"　— {b.note}" if b.note else ""))
        print("\n[预设]")
        for name, spec in PRESETS.items():
            print(f"  {name:<16}{spec['label']}　— {spec['note']}")
    else:
        print(preset(args.preset))


if __name__ == "__main__":
    _cli()
