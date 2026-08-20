#!/usr/bin/env python3
"""素材预处理与成片规范化，全部基于 ffmpeg。

分两头用：

  生成前  给驱动音频加静默头。腾讯要求「开头静默闭口 1-3 秒」，口型跟着音频走，
          所以音频前面接静音，成片开头才是不说话的。
          但模型在静音段经常演「深吸一口气再开口」，看起来很假。
          所以生成后会把静音对应的那几秒画面，换成角色参考图的静止帧
          （嘴唇状态跟你给的照片一致），音轨不动、时长不变。

  生成后  把成片改造成平台收得下的规格：补边到精确 16:9 / 9:16、重采样帧率、
          转 mp4/H.264。生成模型常见的 480×832（比例 1.733）和 25fps 都不达标。

另外提供把短素材做成正倒放无缝循环的能力：阿里云小样本版本来就是取素材里
10~15 秒做正倒放循环，把 10 秒生成结果拼到 60 秒也用同一招，比硬生成 60 秒稳。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from compliance import PROFILES, ComplianceError, probe


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise ComplianceError("需要 ffmpeg，请先安装（apt install ffmpeg / brew install ffmpeg）")


def _run(cmd: list[str], what: str) -> None:
    _require_ffmpeg()
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise ComplianceError(f"{what} 失败：\n{out.stderr.strip()[-800:]}")


def audio_duration(path: str | Path) -> float:
    _require_ffmpeg()
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise ComplianceError(f"读取音频时长失败：{out.stderr.strip()[:200]}")
    return float(out.stdout.strip())


def prepend_silence(audio: str | Path, output: str | Path, seconds: float = 2.0) -> Path:
    """在音频前面接一段静音，让生成的视频开头是闭嘴静默状态。"""
    src, out = Path(audio), Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-t", f"{seconds}", "-i", "anullsrc=r=44100:cl=mono",
         "-i", str(src),
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
         "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(out)],
        f"给 {src.name} 加 {seconds}s 静默头",
    )
    return out


def replace_silent_head(video: str | Path, image: str | Path, output: str | Path,
                        seconds: float, crf: int = 17) -> Path:
    """把视频开头的静音秒数换成角色图静止帧，用来去掉「深吸一口气再说话」。

    音轨原样保留，口型对齐不漂。画面在静音结束处接到生成画面；
    若角色图和生成姿势差太大，接缝处会有轻微跳变，但比演一段吸气自然得多。
    """
    src, img, out = Path(video), Path(image), Path(output)
    if not img.is_file():
        raise ComplianceError(f"找不到角色图 {img}，无法替换静默头")
    info = probe(src)
    if info["duration"] <= seconds + 0.08:
        raise ComplianceError(
            f"视频只有 {info['duration']:.1f}s，不够切掉开头 {seconds:g}s 静默段"
        )

    w, h, fps = info["width"], info["height"], info["fps"]
    vf_still = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={fps},setsar=1,format=yuv420p")

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-loop", "1", "-framerate", f"{fps}", "-t", f"{seconds:.3f}", "-i", str(img),
        "-i", str(src),
        "-filter_complex",
        f"[0:v]{vf_still},setpts=PTS-STARTPTS[still];"
        f"[1:v]trim=start={seconds:.3f},setpts=PTS-STARTPTS[tail];"
        f"[still][tail]concat=n=2:v=1:a=0[v]",
        "-map", "[v]",
    ]
    if info.get("has_audio"):
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-r", f"{fps}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-shortest", str(out),
    ]
    _run(cmd, f"用 {img.name} 替换开头 {seconds:g}s 画面")
    return out


def normalize(video: str | Path, output: str | Path, *, profile: str = "tencent-general",
              fps: float | None = None, short_edge: int | None = None,
              ratio: str | None = None, crf: int = 17, pad_color: str = "black") -> Path:
    """把成片改造成指定平台档位能收的规格。

    尺寸用「先等比缩放再补边」而不是裁切，因为裁切可能把人物头顶或下巴切掉，
    而平台要求脸部完整无遮挡。补边加的是纯色边框，不影响人脸区域。
    """
    if profile not in PROFILES:
        raise ComplianceError(f"未知档位 {profile!r}，可选：{', '.join(PROFILES)}")
    spec = PROFILES[profile]
    src, out = Path(video), Path(output)
    info = probe(src)

    target_fps = fps or (30 if spec.min_fps > 25 else max(25, min(info["fps"], spec.max_fps)))
    target_short = short_edge or spec.min_height

    # 保持横竖屏方向：原来是竖的就出 9:16，横的就出 16:9
    portrait = info["height"] >= info["width"]
    chosen = ratio or ("9:16" if portrait else "16:9")
    if chosen not in spec.allowed_ratios:
        chosen = spec.allowed_ratios[0]

    rw, rh = (int(x) for x in chosen.split(":"))
    if rw < rh:                      # 竖屏：短边是宽
        width, height = target_short, round(target_short * rh / rw)
    else:                            # 横屏：短边是高
        width, height = round(target_short * rw / rh), target_short
    width += width % 2               # H.264 要求偶数边长
    height += height % 2

    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:{pad_color},"
          f"fps={target_fps},setsar=1")

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
           "-vf", vf, "-c:v", "libx264", "-crf", str(crf),
           "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd += ["-c:a", "aac", "-b:a", "192k"] if info["has_audio"] else ["-an"]
    cmd.append(str(out))
    _run(cmd, f"规范化 {src.name} → {width}×{height}@{target_fps:g}fps")
    return out


def loop_boomerang(video: str | Path, output: str | Path, target_seconds: float,
                   crf: int = 17) -> Path:
    """正播 + 倒播反复拼接到目标时长，首尾自然衔接、没有跳帧。

    阿里云小样本版本身就是取素材里 10~15 秒做正倒放循环，所以把 10 秒的生成结果
    拼到 60 秒，和平台内部的做法是一致的，比让模型硬生成 60 秒更不容易崩。
    注意：音轨会被丢掉（倒放音频没有意义），这条只适合无口播的静默素材。
    """
    src, out = Path(video), Path(output)
    info = probe(src)
    unit = info["duration"] * 2                      # 一个正+倒的周期
    rounds = max(1, int(-(-target_seconds // unit)))  # 向上取整
    out.parent.mkdir(parents=True, exist_ok=True)

    _run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-filter_complex",
         f"[0:v]split[f][r];[r]reverse[rv];[f][rv]concat=n=2:v=1:a=0,"
         f"loop=loop={rounds - 1}:size=32767:start=0,"
         f"trim=duration={target_seconds},setpts=N/FRAME_RATE/TB[out]",
         "-map", "[out]", "-an", "-c:v", "libx264", "-crf", str(crf),
         "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(out)],
        f"正倒放循环 {src.name} → {target_seconds:g}s",
    )
    return out


def mux_audio(video: str | Path, audio: str | Path, output: str | Path) -> Path:
    """给视频换上指定音轨，视频流直接复制不重编码。"""
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)],
        "合并音轨",
    )
    return out


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="素材预处理与成片规范化")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sil = sub.add_parser("silence", help="给音频加静默头（满足腾讯「开头闭口 1-3 秒」）")
    p_sil.add_argument("audio")
    p_sil.add_argument("-o", "--output", required=True)
    p_sil.add_argument("--seconds", type=float, default=2.0)

    p_norm = sub.add_parser("normalize", help="把成片改造成平台能收的规格")
    p_norm.add_argument("video")
    p_norm.add_argument("-o", "--output", required=True)
    p_norm.add_argument("--profile", default="tencent-general", choices=list(PROFILES))
    p_norm.add_argument("--fps", type=float)
    p_norm.add_argument("--short-edge", type=int)
    p_norm.add_argument("--ratio", choices=["16:9", "9:16"])
    p_norm.add_argument("--crf", type=int, default=17)

    p_loop = sub.add_parser("loop", help="正倒放循环拼接到目标时长（无音轨）")
    p_loop.add_argument("video")
    p_loop.add_argument("-o", "--output", required=True)
    p_loop.add_argument("--seconds", type=float, required=True)

    p_mux = sub.add_parser("mux", help="给视频换音轨")
    p_mux.add_argument("video")
    p_mux.add_argument("audio")
    p_mux.add_argument("-o", "--output", required=True)

    p_still = sub.add_parser("still-head", help="把开头静默秒数换成角色图静止帧（去掉吸气预备）")
    p_still.add_argument("video")
    p_still.add_argument("image")
    p_still.add_argument("-o", "--output", required=True)
    p_still.add_argument("--seconds", type=float, default=2.0)

    args = parser.parse_args()
    try:
        if args.cmd == "silence":
            out = prepend_silence(args.audio, args.output, args.seconds)
            print(f"已生成 {out}（时长 {audio_duration(out):.1f}s）")
        elif args.cmd == "normalize":
            out = normalize(args.video, args.output, profile=args.profile, fps=args.fps,
                            short_edge=args.short_edge, ratio=args.ratio, crf=args.crf)
            info = probe(out)
            print(f"已生成 {out}（{info['width']}×{info['height']} "
                  f"{info['fps']:g}fps {info['duration']:.1f}s）")
        elif args.cmd == "loop":
            out = loop_boomerang(args.video, args.output, args.seconds)
            print(f"已生成 {out}（{probe(out)['duration']:.1f}s）")
        elif args.cmd == "mux":
            print(f"已生成 {mux_audio(args.video, args.audio, args.output)}")
        elif args.cmd == "still-head":
            out = replace_silent_head(args.video, args.image, args.output, args.seconds)
            info = probe(out)
            print(f"已生成 {out}（{info['width']}×{info['height']} "
                  f"{info['fps']:g}fps {info['duration']:.1f}s）")
    except ComplianceError as exc:
        raise SystemExit(f"错误：{exc}")


if __name__ == "__main__":
    _cli()
