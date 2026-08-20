#!/usr/bin/env python3
"""入库合规校验：拿腾讯云数智人 / 阿里云数字人的素材规格逐条卡成片。

为什么要有这个文件：这批视频不是给人看的成片，而是要喂进平台去训练形象。
平台在「系统检测」环节会按硬指标退料，而生成模型默认输出的 480p/720p、
25fps、480×832 这类尺寸基本都不达标。与其提交后等短信通知被拒，
不如在本地先卡一遍。10 秒筛选成片会卡在「时长」上，这是预期，不是生成失败；
实时日志会列出每一项。开头静默查的是前 2 秒平均音量 ≤ -45dB，所以 --silence
不要短于 2 秒，否则口播会漏进检测窗口。

规格出处：
  腾讯云数智人 形象录制指引 / 定制接口
    https://cloud.tencent.com/document/product/1240/103864
    https://cloud.tencent.com/document/product/1240/96069
  阿里云 2D 小样本视频数字人形象定制指南
    https://help.aliyun.com/zh/avatar/avatar-application/user-guide/
    2d-few-shot-video-digital-human-avatar-customization-guide
  阿里云 2D 高精度视频数字人形象定制指南
    https://help.aliyun.com/zh/avatar/avatar-application/user-guide/
    customized-digital-2d-video-guide

规格可能随平台文档更新，跑之前建议对一眼上面的链接。
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class ComplianceError(Exception):
    pass


@dataclass
class Profile:
    key: str
    label: str
    min_seconds: float
    max_seconds: float
    min_height: int                    # 竖屏取宽、横屏取高中较小的那条边所对应的短边像素
    allowed_ratios: tuple[str, ...]
    min_fps: float
    max_fps: float
    max_gb: float
    containers: tuple[str, ...] = ("mp4", "mov")
    require_silent_head: bool = False  # 开头需要 1~3 秒静默闭口
    require_no_speech: bool = False    # 全程不能说话（阿里小样本版）
    note: str = ""


# 短边像素：1080P 竖屏是 1080×1920，横屏是 1920×1080，两种情况短边都是 1080
PROFILES: dict[str, Profile] = {
    "tencent-general": Profile(
        "tencent-general", "腾讯云数智人 · 通用口型版",
        min_seconds=60, max_seconds=600, min_height=1080,
        allowed_ratios=("16:9", "9:16"), min_fps=25, max_fps=60, max_gb=5,
        require_silent_head=True,
        note="定制接口写明通用口型版 1-10 分钟；开头静默闭口 1-3 秒；全程无剪辑无跳帧",
    ),
    "tencent-broadcast": Profile(
        "tencent-broadcast", "腾讯云数智人 · 播报场景",
        min_seconds=30, max_seconds=600, min_height=1080,
        allowed_ratios=("16:9", "9:16"), min_fps=25, max_fps=60, max_gb=5,
        require_silent_head=True,
        note="录制指引里的下限是 30 秒；腾讯官方明确支持用 AI 生成约一分钟的人物视频来训练",
    ),
    "tencent-hifi": Profile(
        "tencent-hifi", "腾讯云数智人 · 高精版",
        min_seconds=120, max_seconds=600, min_height=2160,
        allowed_ratios=("16:9", "9:16"), min_fps=25, max_fps=60, max_gb=10,
        require_silent_head=True,
        note="高精版必须 4K（3840×2160），大小放宽到 10GB",
    ),
    "aliyun-fewshot": Profile(
        "aliyun-fewshot", "阿里云 2D 小样本视频版",
        min_seconds=10, max_seconds=120, min_height=1080,
        allowed_ratios=("16:9", "9:16"), min_fps=30, max_fps=120, max_gb=25,
        require_no_speech=True,
        note="只要 10 秒~2 分钟的无口播素材，人物全程闭嘴，口型由平台通用口型模型驱动",
    ),
    "aliyun-hifi": Profile(
        "aliyun-hifi", "阿里云 2D 高精度视频版",
        min_seconds=270, max_seconds=330, min_height=1080,
        allowed_ratios=("16:9", "9:16"), min_fps=30, max_fps=120, max_gb=25,
        require_silent_head=True,
        note="要求 5 分钟一镜到底：15 秒静默 + 4~5 分钟口播",
    ),
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    path: Path
    profile: Profile
    checks: list[Check] = field(default_factory=list)
    probe: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> str:
        head = f"{'通过' if self.passed else '不通过'}  {self.path.name}  →  {self.profile.label}"
        lines = [head, "-" * len(head.encode("gbk", "ignore").decode("gbk"))]
        for c in self.checks:
            lines.append(f"  [{'ok' if c.ok else '!!'}] {c.name}: {c.detail}")
        if not self.passed:
            lines.append("")
            lines.append("  修复：python3 mediaprep.py normalize <视频> --profile "
                         f"{self.profile.key} -o fixed.mp4")
        return "\n".join(lines)


def _require_ffprobe() -> None:
    if not shutil.which("ffprobe"):
        raise ComplianceError("需要 ffprobe，请先安装 ffmpeg（apt install ffmpeg / brew install ffmpeg）")


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise ComplianceError("需要 ffmpeg，请先安装（apt install ffmpeg / brew install ffmpeg）")


def probe(path: str | Path) -> dict:
    """用 ffprobe 读取视频的关键参数。"""
    _require_ffprobe()
    p = Path(path)
    if not p.is_file():
        raise ComplianceError(f"找不到视频 {p}")

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise ComplianceError(f"ffprobe 读取 {p.name} 失败: {out.stderr.strip()[:200]}")

    data = json.loads(out.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if video is None:
        raise ComplianceError(f"{p.name} 里没有视频流")

    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if float(den or 0) else 0.0

    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": round(fps, 3),
        "duration": float(data["format"].get("duration") or 0),
        "size_bytes": int(data["format"].get("size") or p.stat().st_size),
        "container": p.suffix.lstrip(".").lower(),
        "vcodec": video.get("codec_name", ""),
        "pix_fmt": video.get("pix_fmt", ""),
        "has_audio": audio is not None,
        "acodec": (audio or {}).get("codec_name", ""),
    }


def _ratio_name(width: int, height: int, tolerance: float = 0.02) -> str:
    """把实际宽高判成 16:9 / 9:16，判不出来就返回约分后的比值字符串。"""
    actual = width / height
    for name, target in (("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:3", 4 / 3), ("3:4", 3 / 4)):
        if abs(actual - target) / target <= tolerance:
            return name
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def head_loudness(path: str | Path, seconds: float = 2.0) -> float | None:
    """开头 N 秒的平均音量（dB）。用来判断腾讯要求的「开头静默闭口」。

    视频和纯音频文件都能传。返回 None 表示没有音轨。
    数值越小越安静，-45dB 以下基本可以认为是静音。
    """
    _require_ffmpeg()
    # volumedetect 的统计结果打在 info 级别，日志级别调低会连它一起吞掉
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-v", "info",
         "-t", str(seconds), "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in out.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    return None


def check(path: str | Path, profile_key: str) -> Report:
    """按指定平台档位逐条校验。"""
    if profile_key not in PROFILES:
        raise ComplianceError(
            f"未知档位 {profile_key!r}，可选：{', '.join(PROFILES)}"
        )
    profile = PROFILES[profile_key]
    info = probe(path)
    report = Report(path=Path(path), profile=profile, probe=info)

    short_edge = min(info["width"], info["height"])
    ratio = _ratio_name(info["width"], info["height"])
    size_gb = info["size_bytes"] / 1024 ** 3

    report.checks.append(Check(
        "时长",
        profile.min_seconds <= info["duration"] <= profile.max_seconds,
        f"{info['duration']:.1f}s（要求 {profile.min_seconds:g}~{profile.max_seconds:g}s）",
    ))
    report.checks.append(Check(
        "分辨率",
        short_edge >= profile.min_height,
        f"{info['width']}×{info['height']}，短边 {short_edge}（要求短边 ≥{profile.min_height}）",
    ))
    report.checks.append(Check(
        "宽高比",
        ratio in profile.allowed_ratios,
        f"{ratio}（要求 {' 或 '.join(profile.allowed_ratios)}）",
    ))
    report.checks.append(Check(
        "帧率",
        profile.min_fps <= info["fps"] <= profile.max_fps,
        f"{info['fps']:g}fps（要求 {profile.min_fps:g}~{profile.max_fps:g}fps）",
    ))
    report.checks.append(Check(
        "封装格式",
        info["container"] in profile.containers,
        f"{info['container']}（要求 {'/'.join(profile.containers)}）",
    ))
    report.checks.append(Check(
        "文件大小",
        size_gb <= profile.max_gb,
        f"{size_gb:.2f}GB（要求 ≤{profile.max_gb:g}GB）",
    ))

    if profile.require_silent_head:
        db = head_loudness(path, 2.0)
        if db is None:
            report.checks.append(Check("开头静默", False, "没有音轨，平台要求音画同步"))
        else:
            report.checks.append(Check(
                "开头静默", db <= -45,
                f"前 2 秒平均音量 {db:.1f}dB（要求 ≤-45dB，即闭口静默 1~3 秒）",
            ))

    if profile.require_no_speech:
        db = head_loudness(path, min(info["duration"], 30))
        detail = "无音轨，符合小样本版「全程闭嘴不说话」" if db is None else \
                 f"检测到音轨（前 30 秒 {db:.1f}dB）。小样本版要求人物全程闭嘴，" \
                 f"素材本身不该有口播；如果只是背景静音轨可忽略此项"
        report.checks.append(Check("无口播", db is None or db <= -45, detail))

    return report


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="校验视频是否满足数字人平台的素材入库要求",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="档位：\n" + "\n".join(
            f"  {k:<20} {v.label}\n{'':22}{v.note}" for k, v in PROFILES.items()
        ),
    )
    parser.add_argument("videos", nargs="+", help="待校验的视频文件")
    parser.add_argument("--profile", default="tencent-general",
                        choices=list(PROFILES), help="平台档位（默认 tencent-general）")
    args = parser.parse_args()

    failed = 0
    for v in args.videos:
        try:
            report = check(v, args.profile)
        except ComplianceError as exc:
            print(f"不通过  {v}: {exc}")
            failed += 1
            continue
        print(report.render())
        print()
        failed += 0 if report.passed else 1

    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    _cli()
