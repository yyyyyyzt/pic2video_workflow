#!/usr/bin/env python3
"""文本转语音：把台词稿合成为驱动音频，全部走 WaveSpeed，只需要一个 API Key。

为什么需要它：数字人视频的时长和口型完全由驱动音频决定，而腾讯通用口型版要求
成片不短于 1 分钟。手工录一段正好 60 秒又不口误的音频，跟直接拍口播一样麻烦，
所以台词稿 → TTS → 驱动音频这一步应该自动化。

配合 mediaprep.prepend_silence 使用：合成完在前面接 1~3 秒静音，
生成出来的视频开头人物就是闭嘴静默的，正好满足腾讯的录制规范。
静默段要靠提示词让人物眨眼、微动；不要用静止帧替换（会很呆）。

用法：
    python3 tts.py --list-voices
    python3 tts.py --script-file prompts/tencent_general_script.txt -o speech.mp3
    python3 tts.py --text "大家好" -o hello.mp3 --voice minimax:Wise_Woman --speed 0.95
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import wsclient


@dataclass(frozen=True)
class Voice:
    key: str          # 命令行用的名字，形如 qwen:Cherry
    model: str        # WaveSpeed model_id
    voice_id: str     # 传给模型的音色标识
    label: str
    gender: str
    note: str = ""


# 只收普通话口播能用的音色。方言音色（粤语/四川/天津等）Qwen3 也有，
# 但数字人形象训练一般要标准普通话，需要时按同样格式加进来即可。
VOICES: list[Voice] = [
    # WaveSpeed 自己托管的 Qwen3，单价最低（$0.005），适合大量试音色
    Voice("qwen:Vivian", "wavespeed-ai/qwen3-tts/text-to-speech", "Vivian", "Vivian · 女声", "女",
          "最便宜。中文自然度够用，适合先试音色再换 MiniMax"),
    Voice("qwen:Dylan", "wavespeed-ai/qwen3-tts/text-to-speech", "Dylan", "Dylan · 男声", "男"),
    Voice("qwen:Eric", "wavespeed-ai/qwen3-tts/text-to-speech", "Eric", "Eric · 男声", "男"),

    # Qwen3-TTS Flash：音色更多（$0.02）
    Voice("qwen:Cherry", "alibaba/qwen3-tts-flash", "Cherry", "芊悦 · 亲和女声", "女",
          "默认。语速平稳，适合知识讲解和产品介绍"),
    Voice("qwen:Ethan", "alibaba/qwen3-tts-flash", "Ethan", "晨煦 · 阳光男声", "男",
          "适合口播和新闻播报"),
    Voice("qwen:Nofish", "alibaba/qwen3-tts-flash", "Nofish", "不吃鱼 · 平实男声", "男"),
    Voice("qwen:Jada", "alibaba/qwen3-tts-flash", "Jada", "阿珍 · 干练女声", "女"),

    # MiniMax：情绪和语速控制最细，中文表现强，贵一点（$0.03）
    Voice("minimax:Wise_Woman", "minimax/speech-02-turbo", "Wise_Woman", "沉稳女声", "女",
          "权威感强，适合正式播报和培训内容"),
    Voice("minimax:Calm_Woman", "minimax/speech-02-turbo", "Calm_Woman", "平静女声", "女"),
    Voice("minimax:Patient_Man", "minimax/speech-02-turbo", "Patient_Man", "耐心男声", "男",
          "语速偏慢，适合安全教育、操作讲解"),
    Voice("minimax:Deep_Voice_Man", "minimax/speech-02-turbo", "Deep_Voice_Man", "低沉男声", "男"),
    Voice("minimax:Elegant_Man", "minimax/speech-02-turbo", "Elegant_Man", "儒雅男声", "男"),
    Voice("minimax:Friendly_Person", "minimax/speech-02-turbo", "Friendly_Person", "亲切中性", "中"),

    # 字节 Seed Speech 2.0：支持 voice_instruction 用自然语言描述语气
    Voice("seed:bonnie_zh", "bytedance/seed-speech-tts-2.0", "bonnie_zh", "Bonnie · 中文女声", "女"),
    Voice("seed:felix_zh", "bytedance/seed-speech-tts-2.0", "felix_zh", "Felix · 中文男声", "男"),
    Voice("seed:celeste_zh", "bytedance/seed-speech-tts-2.0", "celeste_zh", "Celeste · 中文女声", "女"),
]

VOICE_MAP = {v.key: v for v in VOICES}
DEFAULT_VOICE = "qwen:Cherry"

# 中文按字数粗估时长。实测 Qwen3 Cherry：46 字合成约 8.5 秒，约每秒 5.4 字。
# 取 5.2，宁可提示你把台词写长一点，也不要生成出来不够平台的 60 秒门槛。
CHARS_PER_SECOND = 5.2


class TTSError(Exception):
    pass


def resolve_voice(key: str) -> Voice:
    if key in VOICE_MAP:
        return VOICE_MAP[key]
    # 允许只写音色名，不写前缀
    matches = [v for v in VOICES if v.voice_id.lower() == key.lower()]
    if len(matches) == 1:
        return matches[0]
    raise TTSError(
        f"未知音色 {key!r}。用 `python3 tts.py --list-voices` 查看可选项。"
    )


def estimate_seconds(text: str, speed: float = 1.0) -> float:
    """按字数粗估合成后的时长，用来在花钱之前判断够不够平台的时长门槛。"""
    chars = len(text.replace("\n", "").replace(" ", ""))
    return round(chars / CHARS_PER_SECOND / max(speed, 0.1), 1)


def read_script(path: str | Path) -> str:
    """读台词稿。以 # 开头的行按注释忽略，和 prompts/ 的约定一致。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    body = [l.rstrip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not body:
        raise TTSError(f"{path} 里没有有效台词（空行和 # 注释行会被忽略）")
    return "\n".join(body)


def _build_payload(voice: Voice, text: str, speed: float, emotion: str) -> dict:
    """不同 TTS 模型的字段名不一样，在这里各自组装。"""
    if voice.model == "wavespeed-ai/qwen3-tts/text-to-speech":
        return {"text": text, "voice": voice.voice_id, "language": "Chinese"}

    if voice.model == "alibaba/qwen3-tts-flash":
        # Qwen3 Flash 没有语速参数，语速要靠台词本身的标点来控制
        return {"text": text, "voice": voice.voice_id, "language_type": "Chinese"}

    if voice.model.startswith("minimax/"):
        return {
            "text": text, "voice_id": voice.voice_id,
            "speed": speed, "emotion": emotion,
            "language_boost": "Chinese", "format": "mp3", "sample_rate": 44100,
        }

    if voice.model == "bytedance/seed-speech-tts-2.0":
        return {
            "text": text, "voice": voice.voice_id, "speed": speed,
            "language": "zh", "output_format": "mp3", "sample_rate": 44100,
        }

    raise TTSError(f"没有为 {voice.model} 定义 payload 组装规则")


def synthesize(text: str, output: str | Path, *, voice: str = DEFAULT_VOICE,
               speed: float = 1.0, emotion: str = "neutral",
               timeout: int = 600, interval: int = 5,
               on_tick=None) -> tuple[Path, float]:
    """合成语音，返回 (文件路径, 实际扣费)。"""
    text = text.strip()
    if not text:
        raise TTSError("台词为空")

    v = resolve_voice(voice)
    payload = _build_payload(v, text, speed, emotion)

    try:
        before = wsclient.balance()
    except wsclient.WaveSpeedError:
        before = None

    result = wsclient.run(v.model, payload, timeout=timeout, interval=interval,
                          on_tick=on_tick)

    cost = 0.0
    if before is not None:
        try:
            cost = max(0.0, round(before - wsclient.balance(), 4))
        except wsclient.WaveSpeedError:
            cost = 0.0

    # 各家返回的容器不一样（Qwen3 给的是 wav），按真实后缀落盘，
    # 免得下游看着 .mp3 实际是 wav。扩展名不符时 ffmpeg 虽然能靠嗅探读出来，
    # 但上传到别的服务时会因为 content-type 不对被拒。
    url = result["outputs"][0]
    real_ext = Path(url.split("?")[0]).suffix.lower()
    out_path = Path(output)
    if real_ext in (".wav", ".mp3", ".flac", ".opus", ".m4a") and out_path.suffix.lower() != real_ext:
        out_path = out_path.with_suffix(real_ext)

    return wsclient.download(url, out_path), cost


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="把台词稿合成为数字人的驱动音频（走 WaveSpeed，中文音色）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--text", help="直接给台词")
    parser.add_argument("--script-file", help="台词稿文件，# 开头的行是注释")
    parser.add_argument("-o", "--output", default="speech.mp3")
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--speed", type=float, default=1.0,
                        help="语速 0.5~2.0（Qwen3 不支持，用标点控制）")
    parser.add_argument("--emotion", default="neutral",
                        choices=["neutral", "happy", "sad", "angry", "fearful",
                                 "disgusted", "surprised"],
                        help="情绪，仅 MiniMax 系列支持")
    parser.add_argument("--silence", type=float, default=0.0,
                        help="在开头接几秒静音（腾讯要求开头闭口 1~3 秒）")
    parser.add_argument("--list-voices", action="store_true")
    args = parser.parse_args()

    if args.list_voices:
        print(f"{'名字':<24}{'音色':<20}{'性别':<6}{'模型':<34}备注")
        print("-" * 118)
        for v in VOICES:
            print(f"{v.key:<24}{v.label:<20}{v.gender:<6}{v.model:<34}{v.note}")
        print(f"\n默认 {DEFAULT_VOICE}。Qwen3 最便宜，MiniMax 情绪/语速可调，Seed 支持语气指令。")
        return 0

    if not (args.text or args.script_file):
        parser.error("需要 --text 或 --script-file")

    text = args.text or read_script(args.script_file)
    est = estimate_seconds(text, args.speed)
    print(f"台词 {len(text)} 字，预计约 {est:.0f} 秒（按每秒 {CHARS_PER_SECOND:g} 字估算）")
    if est < 60:
        print(f"提示：腾讯通用口型版要求成片不短于 60 秒，当前台词偏短，"
              f"再加约 {int((60 - est) * CHARS_PER_SECOND)} 字比较稳妥。")

    out, cost = synthesize(text, args.output, voice=args.voice, speed=args.speed,
                           emotion=args.emotion,
                           on_tick=lambda s, e: print(f"  {s} ({e}s)", flush=True))

    if args.silence > 0:
        from mediaprep import prepend_silence

        raw = out.with_name(out.stem + "_raw" + out.suffix)
        out.rename(raw)
        out = prepend_silence(raw, Path(args.output).with_suffix(".mp3"), args.silence)
        print(f"已在开头接 {args.silence:g} 秒静音（原始合成保留为 {raw.name}）")

    from mediaprep import audio_duration
    print(f"已生成 {out}，实际时长 {audio_duration(out):.1f} 秒，扣费 ${cost:.4f}")
    return 0


if __name__ == "__main__":
    from wsclient import load_dotenv

    load_dotenv()
    raise SystemExit(_cli())
