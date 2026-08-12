#!/usr/bin/env python3
"""数字人视频后处理：抑制帧间「顿一下」的位置微抖动。

提供多种可独立测试的方法，全部保留原音轨：

    analyze    只诊断不修改：输出抖动指标 + 最抖的时间点（先跑这个）
    track      ★ 光流轨迹平滑 + 整帧仿射补偿（最对症，不影响口型）
    vidstab    ffmpeg vidstab 两遍稳像（通用稳像，强度更猛）
    deflicker  ffmpeg 亮度闪烁抑制（治闪不治位移）
    interp     ffmpeg 运动补偿插帧（不消除抖动，但观感更顺）
    chain      按逗号顺序串联多个方法，例如 track,deflicker

用法：
    python3 stabilize.py in.mp4 --method analyze
    python3 stabilize.py in.mp4 -o out.mp4 --method track
    python3 stabilize.py in.mp4 -o out.mp4 --method track --compare cmp.mp4

原理：抖动的本质是人物位置/尺度的**高频加速度尖峰**。track 方法先用稀疏光流
估计每帧的相似变换（平移/旋转/缩放），累积成运动轨迹，对轨迹做低通平滑，再把
「实际轨迹 → 平滑轨迹」的差值作为补偿量 warp 回去。补偿是**整帧**的，所以口型、
表情等局部形变完全不受影响。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

# --------------------------------------------------------------------------- 基础工具


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("错误：需要 ffmpeg / ffprobe，请先安装（apt install ffmpeg 或 brew install ffmpeg）")


def run(cmd: list[str], quiet: bool = True) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        sys.exit("命令执行失败: " + " ".join(cmd[:4]) + " ...\n" + "\n".join(tail))


def probe(path: str) -> dict:
    """返回 {width, height, fps, nb_frames, duration, has_audio}。"""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"无法读取视频信息: {path}")
    info = json.loads(out.stdout)

    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        sys.exit(f"文件里没有视频流: {path}")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])

    rate = video.get("avg_frame_rate") or "0/1"
    num, _, den = rate.partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps or 25.0,
        # 保留分数形式（如 30000/1001），交给 ffmpeg 才不会因取整产生音画漂移
        "fps_frac": rate if fps else "25/1",
        "duration": float(info["format"].get("duration", 0.0)),
        "has_audio": has_audio,
    }


def open_frame_writer(source: str, output: str, meta: dict, crf: int) -> subprocess.Popen:
    """打开一个 ffmpeg 管道：stdin 收 BGR 原始帧，同时把原音轨原样并入。

    比 cv2.VideoWriter 更可靠——不会在收尾时丢帧，也少一次中间有损编码。
    """
    require_ffmpeg()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{meta['width']}x{meta['height']}",
        "-r", meta["fps_frac"], "-i", "pipe:0",
    ]
    if meta["has_audio"]:
        cmd += ["-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
    cmd += [
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def parse_roi(spec: str, width: int, height: int) -> tuple[int, int, int, int]:
    """ROI 用于**估计**运动（补偿仍作用于整帧）。

    支持 full / upper / center，或 x,y,w,h（0~1 比例，或绝对像素）。
    """
    if spec == "full":
        return 0, 0, width, height
    if spec == "upper":  # 口播人物的头肩通常在画面上部中央
        return int(width * 0.20), 0, int(width * 0.60), int(height * 0.65)
    if spec == "center":
        return int(width * 0.25), int(height * 0.15), int(width * 0.50), int(height * 0.70)

    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 4:
        sys.exit(f"错误：--roi 需为 full/upper/center 或 x,y,w,h，收到 {spec!r}")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        sys.exit(f"错误：--roi 数值无法解析: {spec!r}")
    if all(v <= 1.0 for v in vals):
        x, y, w, h = vals[0] * width, vals[1] * height, vals[2] * width, vals[3] * height
    else:
        x, y, w, h = vals
    x, y = max(0, int(x)), max(0, int(y))
    w, h = min(int(w), width - x), min(int(h), height - y)
    if w < 32 or h < 32:
        sys.exit("错误：--roi 区域太小")
    return x, y, w, h


# --------------------------------------------------------------------------- 运动估计


def estimate_motion(path: str, roi_spec: str, verbose: bool = True) -> tuple[np.ndarray, dict]:
    """逐帧估计相似变换增量，返回 (N-1, 4) 的 [dx, dy, dangle, dscale] 与视频信息。"""
    meta = probe(path)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        sys.exit(f"OpenCV 无法打开视频: {path}")

    rx, ry, rw, rh = parse_roi(roi_spec, meta["width"], meta["height"])
    mask = np.zeros((meta["height"], meta["width"]), dtype=np.uint8)
    mask[ry : ry + rh, rx : rx + rw] = 255

    feature_args = dict(maxCorners=600, qualityLevel=0.01, minDistance=8, blockSize=7)
    lk_args = dict(
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )

    ok, prev = cap.read()
    if not ok:
        sys.exit("视频没有可读帧")
    prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)

    deltas: list[list[float]] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        pts_prev = cv2.goodFeaturesToTrack(prev_gray, mask=mask, **feature_args)
        delta = [0.0, 0.0, 0.0, 0.0]
        if pts_prev is not None and len(pts_prev) >= 12:
            pts_next, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, pts_prev, None, **lk_args)
            if pts_next is not None:
                sel = status.ravel() == 1
                a, b = pts_prev[sel], pts_next[sel]
                if len(a) >= 12:
                    matrix, _ = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=2.0)
                    if matrix is not None:
                        dx, dy = float(matrix[0, 2]), float(matrix[1, 2])
                        angle = float(np.arctan2(matrix[1, 0], matrix[0, 0]))
                        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
                        delta = [dx, dy, angle, scale - 1.0]
        deltas.append(delta)
        prev_gray = gray

        if verbose and len(deltas) % 200 == 0:
            print(f"  已分析 {len(deltas)} 帧…")

    cap.release()
    if not deltas:
        sys.exit("视频帧数不足，无法估计运动")
    meta["roi"] = (rx, ry, rw, rh)
    meta["frames"] = len(deltas) + 1
    return np.asarray(deltas, dtype=np.float64), meta


def jitter_metrics(deltas: np.ndarray, fps: float) -> dict:
    """抖动量化：位移的一阶（速度）与二阶（加速度）统计。

    「顿一下」在数值上就是加速度的孤立尖峰，所以 p99/max 比均值更有判别力。
    """
    speed = np.hypot(deltas[:, 0], deltas[:, 1])
    accel = np.abs(np.diff(speed, prepend=speed[:1]))
    return {
        "speed_p50": float(np.percentile(speed, 50)),
        "speed_p95": float(np.percentile(speed, 95)),
        "accel_p50": float(np.percentile(accel, 50)),
        "accel_p95": float(np.percentile(accel, 95)),
        "accel_p99": float(np.percentile(accel, 99)),
        "accel_max": float(accel.max()),
        "accel_mean": float(accel.mean()),
        "_accel": accel,
        "_speed": speed,
        "_fps": fps,
    }


def cmd_analyze(path: str, roi_spec: str, top: int) -> None:
    print(f"分析 {path}")
    deltas, meta = estimate_motion(path, roi_spec)
    m = jitter_metrics(deltas, meta["fps"])
    rx, ry, rw, rh = meta["roi"]

    print(f"\n分辨率 {meta['width']}x{meta['height']} | {meta['fps']:.2f} fps | "
          f"{meta['frames']} 帧 | {meta['duration']:.1f}s | 音轨 {'有' if meta['has_audio'] else '无'}")
    print(f"运动估计 ROI: x={rx} y={ry} w={rw} h={rh}（{roi_spec}）\n")

    print("帧间位移（像素/帧）  中位数 %.3f   p95 %.3f" % (m["speed_p50"], m["speed_p95"]))
    print("抖动加速度（像素/帧²）中位数 %.3f   p95 %.3f   p99 %.3f   最大 %.3f"
          % (m["accel_p50"], m["accel_p95"], m["accel_p99"], m["accel_max"]))

    accel = m["_accel"]
    ratio = m["accel_p99"] / max(m["accel_p50"], 1e-6)
    print(f"\n尖峰比（p99/中位数）= {ratio:.1f}")

    # 用 MAD 定阈值：对重尾分布比标准差稳健得多
    mad = float(np.median(np.abs(accel - np.median(accel)))) or 1e-6
    threshold = float(np.median(accel)) + 8.0 * 1.4826 * mad
    spikes = np.flatnonzero(accel > max(threshold, m["accel_p50"] * 4))
    spike_ratio = len(spikes) / len(accel) * 100

    # 突跳往往是「跳出去 + 跳回来」，在加速度上表现为相邻两帧同时超阈值
    groups: list[list[int]] = []
    for idx in spikes.tolist():
        if groups and idx - groups[-1][-1] <= 2:
            groups[-1].append(idx)
        else:
            groups.append([idx])
    paired = sum(1 for g in groups if len(g) >= 2)

    print(f"超阈值帧 {len(spikes)} / {len(accel)}（{spike_ratio:.2f}%），聚成 {len(groups)} 处事件，"
          f"其中 {paired} 处为相邻成对")

    # 估计消除这些尖峰实际需要多大补偿位移，用来推荐 --max-shift
    traj = np.cumsum(deltas, axis=0)
    need = float(np.hypot(*(median_filter(traj, 5) - traj).T[:2]).max())
    suggest_shift = max(6, int(np.ceil(need * 1.4)))

    isolated = len(groups) > 0 and spike_ratio < 2.0
    print()
    if isolated:
        print(f"判定：**孤立突跳**（仅 {spike_ratio:.2f}% 的帧异常，其余 {100 - spike_ratio:.1f}% 本来就平顺）")
        if paired:
            print(f"      {paired} 处成对出现 → 典型的「跳出去又跳回来」的单帧位置突跳")
        print("      → 用中值滤波精准打击，不要用高斯（会把整段的自然微动一起抹平）")
        print(f"\n  推荐命令：\n    python3 stabilize.py <输入> -o fixed.mp4 --method track \\\n"
              f"        --smooth-mode median --radius 5 --max-shift {suggest_shift}")
        print(f"\n  若处理后还能看出残留，再收紧一档：\n"
              f"    python3 stabilize.py <输入> -o fixed.mp4 --method track \\\n"
              f"        --smooth-mode hybrid --radius 3 --max-shift {suggest_shift}")
    elif ratio >= 4:
        print("判定：**持续性抖动**（异常不集中）→ 适合高斯低通")
        print(f"\n  推荐命令：\n    python3 stabilize.py <输入> -o fixed.mp4 --method track \\\n"
              f"        --smooth-mode gaussian --radius 15 --max-shift {suggest_shift}")
    else:
        print("判定：运动本身平顺，没有明显抖动源。")
        print("      若仍觉得卡顿，多半是帧率观感问题 → python3 stabilize.py <输入> -o fixed.mp4 --method interp")

    print(f"\n  （消除尖峰约需 {need:.1f}px 补偿，所以 --max-shift 建议 ≥ {suggest_shift}；"
          f"默认 12 会把大尖峰削一半）")

    order = np.argsort(accel)[::-1][:top]
    print(f"\n最抖的 {top} 个时刻（按加速度排序，可直接跳到该秒逐帧看）：")
    for rank, idx in enumerate(sorted(order.tolist()), 1):
        t = (idx + 1) / meta["fps"]
        print(f"  {rank:>2d}. 第 {idx + 1:>5d} 帧  t = {t:6.2f}s   加速度 {accel[idx]:6.3f}")


# --------------------------------------------------------------------------- track


def median_filter(traj: np.ndarray, radius: int) -> np.ndarray:
    """滑动中值：专门对付孤立的单帧突跳。

    中值对脉冲免疫——窗口里只要多数帧正常，突跳那一帧就被直接替换掉，而缓慢的
    真实运动（多数帧的共识）被原样保留。这是它比高斯更适合「偶尔顿一下」的原因。
    """
    if radius < 1:
        return traj.copy()
    out = np.empty_like(traj)
    for c in range(traj.shape[1]):
        padded = np.pad(traj[:, c], (radius, radius), mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, radius * 2 + 1)
        out[:, c] = np.median(windows, axis=-1)
    return out


def convolve_filter(traj: np.ndarray, radius: int, mode: str) -> np.ndarray:
    """高斯 / 均值低通：压掉持续性高频抖动，代价是自然微动也会被削弱。"""
    if radius < 1:
        return traj.copy()
    if mode == "gaussian":
        offsets = np.arange(-radius, radius + 1)
        kernel = np.exp(-(offsets ** 2) / (2 * (radius / 2.0) ** 2))
    else:
        kernel = np.ones(radius * 2 + 1)
    kernel /= kernel.sum()

    out = np.empty_like(traj)
    for c in range(traj.shape[1]):
        padded = np.pad(traj[:, c], (radius, radius), mode="edge")
        out[:, c] = np.convolve(padded, kernel, mode="valid")
    return out


def smooth_trajectory(traj: np.ndarray, radius: int, mode: str, despike_radius: int = 5) -> np.ndarray:
    """按模式平滑累积轨迹。

    median  只去孤立突跳，最大程度保留自然微动（推荐用于「偶尔顿一下」）
    hybrid  先中值去脉冲，再轻度高斯收尾（尖峰清得更干净，微动损失居中）
    gaussian/box  传统低通，适合持续性抖动
    """
    if mode == "median":
        return median_filter(traj, radius)
    if mode == "hybrid":
        return convolve_filter(median_filter(traj, despike_radius), radius, "gaussian")
    return convolve_filter(traj, radius, mode)


def stabilize_track(
    src: str,
    output: str,
    roi_spec: str,
    radius: int,
    mode: str,
    max_shift: float,
    zoom: float,
    lock_scale: bool,
    lock_rotation: bool,
    crf: int = 17,
    despike_radius: int = 5,
) -> None:
    deltas, meta = estimate_motion(src, roi_spec)
    before = jitter_metrics(deltas, meta["fps"])

    traj = np.cumsum(deltas, axis=0)
    smoothed = smooth_trajectory(traj, radius, mode, despike_radius)
    correction = smoothed - traj  # 需要施加的补偿量

    limit = max_shift if max_shift > 0 else float("inf")
    norm = np.hypot(correction[:, 0], correction[:, 1])
    over = norm > limit
    if np.any(over):
        factor = limit / norm[over]
        correction[over, 0] *= factor
        correction[over, 1] *= factor
        print(f"注意：{int(over.sum())} 帧的补偿被 --max-shift={max_shift:g} 限制"
              f"（最大需要 {norm.max():.1f}px）；如尖峰未消尽请调高该值")
    if lock_rotation:
        correction[:, 2] = 0.0
    if lock_scale:
        correction[:, 3] = 0.0

    width, height = meta["width"], meta["height"]
    cx, cy = width / 2.0, height / 2.0

    cap = cv2.VideoCapture(src)
    proc = open_frame_writer(src, output, meta, crf)

    residual: list[float] = []
    written = 0
    print(f"应用补偿（平滑窗口 ±{radius} 帧，{mode}）…")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            # 第 0 帧无增量，保持原样；其余用 correction[idx-1]
            if written == 0:
                dx = dy = angle = dscale = 0.0
            else:
                dx, dy, angle, dscale = correction[min(written - 1, len(correction) - 1)]
            residual.append(float(np.hypot(dx, dy)))

            scale = (1.0 + dscale) * zoom
            matrix = cv2.getRotationMatrix2D((cx, cy), np.degrees(angle), scale)
            matrix[0, 2] += dx
            matrix[1, 2] += dy
            warped = cv2.warpAffine(
                frame, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
            )
            proc.stdin.write(np.ascontiguousarray(warped).tobytes())
            written += 1
    finally:
        cap.release()
        if proc.stdin:
            proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
        if proc.wait() != 0:
            sys.exit("ffmpeg 写出失败：\n" + "\n".join(stderr.strip().splitlines()[-10:]))

    if written != meta["frames"]:
        print(f"警告：写出 {written} 帧，估计阶段读到 {meta['frames']} 帧")
    print(f"帧数 {written}（与输入一致，音画不会漂移）")

    smoothed_speed = np.hypot(np.diff(smoothed[:, 0], prepend=smoothed[0, 0]),
                              np.diff(smoothed[:, 1], prepend=smoothed[0, 1]))
    after_accel = np.abs(np.diff(smoothed_speed, prepend=smoothed_speed[:1]))
    print(f"\n抖动加速度 p99：{before['accel_p99']:.3f} → {np.percentile(after_accel, 99):.3f}（预估）")
    print(f"最大补偿位移：{max(residual):.2f} px（zoom={zoom} 用于裁掉边缘）")
    print(f"已输出 {output}")


# --------------------------------------------------------------------------- ffmpeg 系


def stabilize_vidstab(src: str, output: str, shakiness: int, smoothing: int, zoom: float, crf: int = 17) -> None:
    require_ffmpeg()
    tmp_dir = Path(tempfile.mkdtemp(prefix="kling_vidstab_"))
    trf = tmp_dir / "transforms.trf"
    print("vidstab 第 1 遍：检测运动…")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", f"vidstabdetect=shakiness={shakiness}:accuracy=15:result={trf}",
         "-f", "null", "-"])
    print("vidstab 第 2 遍：应用补偿…")
    zoom_pct = round((zoom - 1.0) * 100, 2)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", f"vidstabtransform=input={trf}:smoothing={smoothing}:zoom={zoom_pct}"
                ":optzoom=0:interpol=bicubic,unsharp=5:5:0.4:3:3:0.2",
         "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", output])
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"已输出 {output}")


def stabilize_deflicker(src: str, output: str, size: int, crf: int = 17) -> None:
    require_ffmpeg()
    print(f"deflicker（窗口 {size} 帧）…")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", f"deflicker=size={size}:mode=am,deband=1thr=0.02:2thr=0.02:3thr=0.02",
         "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", output])
    print(f"已输出 {output}")


def stabilize_interp(src: str, output: str, fps: float, crf: int = 17) -> None:
    require_ffmpeg()
    meta = probe(src)
    target = fps if fps > 0 else meta["fps"] * 2
    print(f"运动补偿插帧 {meta['fps']:.2f} → {target:.2f} fps…")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", f"minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
         "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", output])
    print(f"已输出 {output}")


def make_compare(original: str, processed: str, output: str) -> None:
    """左右并排对比视频（左=原始，右=处理后），方便肉眼判断是否值得。"""
    require_ffmpeg()
    print("生成对比视频…")
    labelled = (
        "[0:v]drawtext=text='BEFORE':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.5[a];"
        "[1:v]drawtext=text='AFTER':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.5[b];"
        "[a][b]hstack=inputs=2[v]"
    )
    base = ["ffmpeg", "-y", "-loglevel", "error", "-i", original, "-i", processed]
    tail = ["-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "copy", output]

    probe_run = subprocess.run(base + ["-filter_complex", labelled] + tail, capture_output=True, text=True)
    if probe_run.returncode != 0:
        # 缺字体/freetype 时退回无文字标注的并排画面
        print("  （drawtext 不可用，改用无标注并排）")
        run(base + ["-filter_complex", "[0:v][1:v]hstack=inputs=2[v]"] + tail)
    print(f"已输出对比视频 {output}（左=原始，右=处理后）")


# --------------------------------------------------------------------------- CLI


def apply_method(method: str, src: str, output: str, args: argparse.Namespace) -> None:
    if method == "track":
        stabilize_track(
            src, output, args.roi, args.radius, args.smooth_mode,
            args.max_shift, args.zoom, args.lock_scale, args.lock_rotation, args.crf,
            args.despike_radius,
        )
    elif method == "vidstab":
        stabilize_vidstab(src, output, args.shakiness, args.smoothing, args.zoom, args.crf)
    elif method == "deflicker":
        stabilize_deflicker(src, output, args.deflicker_size, args.crf)
    elif method == "interp":
        stabilize_interp(src, output, args.interp_fps, args.crf)
    else:
        sys.exit(f"chain 中不支持的方法: {method}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="数字人视频后处理：抑制帧间位置微抖动（保留口型与音轨）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="输入视频")
    parser.add_argument("-o", "--output", help="输出视频（analyze 模式不需要）")
    parser.add_argument("--method", default="analyze",
                        help="analyze / track / vidstab / deflicker / interp，或 chain 用逗号串联如 track,deflicker")
    parser.add_argument("--compare", help="额外生成左右对比视频到该路径")

    g = parser.add_argument_group("track 参数（推荐先调这个）")
    g.add_argument("--roi", default="full", help="运动估计区域：full / upper / center / x,y,w,h（比例或像素）")
    g.add_argument("--radius", type=int, default=15, help="平滑窗口半径（帧）。越大越稳但真实运动也会被削弱")
    g.add_argument("--smooth-mode", choices=["median", "hybrid", "gaussian", "box"], default="median",
                   help="median=只去孤立突跳(推荐配 --radius 5) / hybrid=中值+轻度高斯 / "
                        "gaussian,box=传统低通，适合持续性抖动")
    g.add_argument("--despike-radius", type=int, default=5, help="hybrid 模式下中值去脉冲的窗口半径")
    g.add_argument("--max-shift", type=float, default=30.0,
                   help="单帧最大补偿位移（像素），0=不限制。太小会把大尖峰削掉一半")
    g.add_argument("--zoom", type=float, default=1.02, help="轻微放大以裁掉补偿产生的边缘（1.0=不放大）")
    g.add_argument("--lock-scale", action="store_true", help="不补偿缩放（只修平移/旋转）")
    g.add_argument("--lock-rotation", action="store_true", help="不补偿旋转")

    g2 = parser.add_argument_group("其他方法参数")
    g2.add_argument("--shakiness", type=int, default=6, help="vidstab 抖动强度 1~10")
    g2.add_argument("--smoothing", type=int, default=20, help="vidstab 平滑帧数")
    g2.add_argument("--deflicker-size", type=int, default=5, help="deflicker 窗口帧数 2~129")
    g2.add_argument("--interp-fps", type=float, default=0, help="插帧目标帧率，0=原帧率×2")
    g2.add_argument("--top", type=int, default=10, help="analyze 列出最抖的前 N 个时刻")
    g2.add_argument("--crf", type=int, default=17, help="输出编码质量，越小越好越大文件（17≈视觉无损）")

    args = parser.parse_args()
    if not Path(args.input).is_file():
        sys.exit(f"错误：找不到输入视频 {args.input}")

    if args.method == "analyze":
        cmd_analyze(args.input, args.roi, args.top)
        return

    if not args.output:
        sys.exit("错误：该方法需要 -o/--output 指定输出路径")

    methods = [m.strip() for m in args.method.split(",") if m.strip()]
    unknown = [m for m in methods if m not in ("track", "vidstab", "deflicker", "interp")]
    if unknown:
        sys.exit(f"错误：未知方法 {unknown}，可用: track / vidstab / deflicker / interp（逗号串联）")

    if len(methods) == 1:
        apply_method(methods[0], args.input, args.output, args)
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="kling_chain_"))
        current = args.input
        try:
            for i, method in enumerate(methods):
                is_last = i == len(methods) - 1
                dest = args.output if is_last else str(tmp_dir / f"step{i}_{method}.mp4")
                print(f"\n=== 步骤 {i + 1}/{len(methods)}: {method} ===")
                apply_method(method, current, dest, args)
                current = dest
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if args.compare:
        make_compare(args.input, args.output, args.compare)


if __name__ == "__main__":
    main()
