#!/usr/bin/env python3
"""MiniMax H3 长视频串接工具 —— 用 4~15 秒的分段拼出 70 秒级成片。

H3 单次最长 15 秒，本工具把长视频拆成多段串行生成再拼接，并提供两种串接策略
（互斥，见下）。所有花钱的操作都可以先用 --dry-run 预演。

    frame 链（画面连贯优先）
        段 i 的 first_frame = 段 i-1 的末帧 → 衔接处画面天然连续
        代价：H3 规定 first_frame/last_frame 与 reference_* 互斥，
              所以这条链**无法**再用参考图锚定身份，长链会累积漂移

    reference 链（身份一致优先）
        每段都传同一批 reference_image（角色图 + 上一段末帧）
        好处：身份锚点始终是原图，不累积漂移
        代价：段边界画面会跳变，需要转场掩盖（--transition）

用法（成本从低到高；分镜已放在 prompts/，不必手写 /tmp 文件）：
    python3 minimax_h3.py plan   --storyboard prompts/h3_electricity_safety_70s.txt
    python3 minimax_h3.py single --prompt-file prompts/h3_electricity_safety_single.txt --duration 4
    python3 minimax_h3.py chain  --storyboard prompts/h3_electricity_safety_probe.txt \
        --image face.png --dry-run
    python3 minimax_h3.py chain  --storyboard prompts/h3_electricity_safety_probe.txt \
        --image face.png --mode frame -o try_frame.mp4

文档：https://platform.minimaxi.com/docs/guides/video-generation
方案取舍与口播场景的结论：见 MINIMAX_H3.md
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://api.minimaxi.com"
CREATE_PATH = "/v2/video_generation"
QUERY_PATH = "/v2/query/video_generation"
MODEL = "MiniMax-H3"

MIN_SEG, MAX_SEG = 4, 15          # H3 单段时长上限（整数秒）
MAX_REFERENCE_IMAGES = 9
FREE_IMAGE_QUOTA = 5              # 超出部分按张计费
FPS = 24                          # H3 输出帧率

# 刊例价（元）。会变，用 --price-* 覆盖即可。
PRICE_PER_SECOND = {"768P": 0.50, "2K": 0.80}
PRICE_PER_EXTRA_IMAGE = 0.20
PRICE_REGENERATE_2K = 0.30

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


class H3Error(Exception):
    """可预期的失败：配置缺失、参数不合法、API 报错。"""


class H3ArgumentParser(argparse.ArgumentParser):
    """把常见的 -output 写成单横线时，给出能看懂的提示。"""

    def error(self, message: str) -> None:
        if "-output" in message or "unrecognized arguments: -o" in message:
            message += "\n提示：输出路径请用 --output 或 -o，例如 -o try_frame.mp4"
        super().error(message)


# --------------------------------------------------------------------------- 基础


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise H3Error("需要 ffmpeg / ffprobe（apt install ffmpeg 或 brew install ffmpeg）")


def run_ff(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise H3Error(f"ffmpeg 失败: {' '.join(cmd[:5])} …\n{tail}")


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        raise H3Error("未配置 MINIMAX_API_KEY（见 .env.example）\n"
                      "获取：https://platform.minimaxi.com/user-center/basic-information/interface-key")
    return key


def to_data_uri(path: str | Path) -> str:
    """本地图片 → data URI。H3 支持 data:image/<格式>;base64,<...>，
    抽出来的帧只有几百 KB，比先上传到公网省事。"""
    p = Path(path)
    if not p.is_file():
        raise H3Error(f"找不到图片 {p}")
    if p.suffix.lower() not in IMAGE_EXTS:
        raise H3Error(f"图片格式不支持 {p.suffix}（支持 {'/'.join(sorted(IMAGE_EXTS))}）")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"


def image_ref(source: str) -> str:
    """公网 URL / mm_file:// 原样透传；本地文件转 data URI。"""
    if source.startswith(("http://", "https://", "mm_file://", "data:")):
        return source
    return to_data_uri(source)


# --------------------------------------------------------------------------- API


def _post(payload: dict, key: str, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(f"{BASE_URL}{CREATE_PATH}", json=payload, timeout=180,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 ** attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            raise H3Error(f"返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}") from None

        if resp.status_code != 200 or "task_id" not in data:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            message = err.get("message") or json.dumps(data, ensure_ascii=False)[:300]
            # 429 限流值得重试，其余（余额不足、敏感内容、参数错）重试没意义
            if resp.status_code == 429 and attempt < retries - 1:
                last = H3Error(message)
                time.sleep(5 * (attempt + 1))
                continue
            raise H3Error(f"创建任务失败 (HTTP {resp.status_code}): {message}")
        return data

    raise H3Error(f"网络请求失败（重试 {retries} 次）: {last}")


def create_task(content: list[dict], duration: int, resolution: str,
                ratio: str | None, key: str) -> str:
    payload: dict = {"model": MODEL, "content": content,
                     "duration": int(duration), "resolution": resolution}
    # 文生视频必须显式给 ratio 且不能 adaptive；图生视频恒为 adaptive（传了会被忽略）
    if ratio:
        payload["ratio"] = ratio
    return _post(payload, key)["task_id"]


def wait_task(task_id: str, key: str, interval: int = 10, timeout: int = 3600,
              quiet: bool = False) -> str:
    """轮询直到 succeeded，返回成片下载地址。"""
    deadline = time.time() + timeout
    started = time.time()
    last_status = ""
    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = requests.get(f"{BASE_URL}{QUERY_PATH}/{task_id}", timeout=60,
                                headers={"Authorization": f"Bearer {key}"})
            task = resp.json().get("task") or {}
        except (requests.RequestException, ValueError):
            continue  # 查询抖动不该让整段任务失败，下一轮再试

        status = task.get("status", "")
        if status != last_status and not quiet:
            print(f"    [{int(time.time() - started):>4d}s] {status}")
            last_status = status

        if status == "succeeded":
            url = (task.get("content") or {}).get("url")
            if not url:
                raise H3Error("任务成功但没有返回下载地址")
            return url
        if status in ("failed", "cancelled"):
            raise H3Error(f"任务 {status}: {task.get('error') or '(接口未给原因)'}")

    raise H3Error(f"等待超时（{timeout}s），task_id={task_id} 可稍后手动查询")


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


# --------------------------------------------------------------------------- 分段规划


def plan_segments(total: int, prefer: int = 14) -> list[int]:
    """把总时长拆成若干 4~15 秒的整数段，尽量均匀且都不小于 4 秒。"""
    if total < MIN_SEG:
        raise H3Error(f"总时长 {total}s 小于单段下限 {MIN_SEG}s")
    if total <= MAX_SEG:
        return [total]                           # 一段就能装下，不必拆
    prefer = max(MIN_SEG, min(MAX_SEG, prefer))
    count = max(1, -(-total // prefer))          # 向上取整
    base, extra = divmod(total, count)

    if base < MIN_SEG:                           # 段数太多导致每段过短，减段
        count = total // MIN_SEG
        base, extra = divmod(total, count)
    segments = [base + (1 if i < extra else 0) for i in range(count)]

    if any(s > MAX_SEG for s in segments):       # 反向兜底：段数太少导致超长
        count += 1
        base, extra = divmod(total, count)
        segments = [base + (1 if i < extra else 0) for i in range(count)]
    return segments


def list_bundled_storyboards() -> list[str]:
    d = Path(__file__).resolve().parent / "prompts"
    if not d.is_dir():
        return []
    names = []
    for p in sorted(d.glob("h3_*.txt")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if not any(line.lstrip().startswith("##") for line in text.splitlines()):
            continue
        try:
            names.append(str(p.relative_to(Path.cwd())))
        except ValueError:
            names.append(str(p))
    return names


def add_output_arg(parser: argparse.ArgumentParser, default: str) -> None:
    """同时接受 --output、-o 和误写的 -output。"""
    parser.add_argument("-o", "--output", "-output", default=default, help="输出视频路径")


def load_storyboard(path: str) -> list[dict]:
    """读分镜脚本。格式：

        ## 12s | 段落标题（时长可省略，默认走 --prefer-duration）
        这一段的提示词，可多行
        （空行或下一个 ## 结束本段）

    以 # 开头但不是 ## 的行是注释。
    """
    p = Path(path)
    if not p.is_file():
        bundled = list_bundled_storyboards()
        hint = ""
        if bundled:
            hint = "。仓库里现成的分镜（直接用这些路径，不必再写 /tmp 文件）：\n  " + "\n  ".join(bundled)
        raise H3Error(f"找不到分镜脚本 {path}{hint}")

    segments: list[dict] = []
    current: dict | None = None
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("##"):
            header = stripped.lstrip("#").strip()
            duration, title = None, header
            if "|" in header:
                left, _, right = header.partition("|")
                title = right.strip()
                left = left.strip().rstrip("sS秒")
                if left.isdigit():
                    duration = int(left)
            elif header.rstrip("sS秒").isdigit():
                duration, title = int(header.rstrip("sS秒")), ""
            current = {"duration": duration, "title": title, "prompt": ""}
            segments.append(current)
            continue
        if stripped.startswith("#") or current is None:
            continue
        current["prompt"] += (line + "\n")

    for seg in segments:
        seg["prompt"] = seg["prompt"].strip()
    segments = [s for s in segments if s["prompt"]]
    if not segments:
        raise H3Error(f"分镜脚本里没有解析到任何段落（需要 ## 开头的段标记）: {path}")
    for seg in segments:
        if seg["duration"] is not None and not MIN_SEG <= seg["duration"] <= MAX_SEG:
            raise H3Error(f"段落「{seg['title']}」时长 {seg['duration']}s 超出 {MIN_SEG}~{MAX_SEG}s")
    return segments


def estimate_cost(segments: list[int], resolution: str, images_per_call: int,
                  regenerate_2k: bool = False) -> dict:
    total_sec = sum(segments)
    per_sec = PRICE_PER_SECOND.get(resolution, PRICE_PER_SECOND["768P"])
    video = total_sec * per_sec
    extra_images = max(0, images_per_call - FREE_IMAGE_QUOTA) * len(segments)
    images = extra_images * PRICE_PER_EXTRA_IMAGE
    regen = total_sec * PRICE_REGENERATE_2K if regenerate_2k else 0.0
    return {"seconds": total_sec, "segments": len(segments),
            "video": round(video, 2), "images": round(images, 2),
            "regenerate": round(regen, 2), "total": round(video + images + regen, 2)}


# --------------------------------------------------------------------------- 视频处理


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def extract_last_frame(video: Path, dest: Path) -> Path:
    """抽最后一帧作为下一段的首帧/参考图。

    用 sseof 定位到结尾前一点再取最后一帧，比 -sseof -0.04 更稳
    （某些编码下精确取末帧会拿到空帧）。
    """
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ff(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.5", "-i", str(video),
            "-update", "1", "-q:v", "2", "-frames:v", "1", str(dest)])
    if not dest.is_file() or dest.stat().st_size == 0:
        raise H3Error(f"末帧抽取失败: {video}")
    return dest


def concat_videos(parts: list[Path], output: Path, transition: float = 0.0,
                  audio: str = "keep", external_audio: str | None = None) -> None:
    """拼接分段。transition>0 时用 xfade 交叉溶解掩盖边界跳变。"""
    require_ffmpeg()
    if not parts:
        raise H3Error("没有可拼接的分段")
    output.parent.mkdir(parents=True, exist_ok=True)

    if len(parts) == 1 and transition <= 0:
        shutil.copy(parts[0], output)
    elif transition > 0:
        # xfade 需要逐对做，且每次转场会吃掉 transition 秒总时长
        inputs: list[str] = []
        for part in parts:
            inputs += ["-i", str(part)]
        filters, last_v, last_a = [], "[0:v]", "[0:a]"
        offset = probe_duration(parts[0])
        for i in range(1, len(parts)):
            offset -= transition
            vout, aout = f"[v{i}]", f"[a{i}]"
            filters.append(f"{last_v}[{i}:v]xfade=transition=fade:duration={transition}"
                           f":offset={max(0.1, offset):.3f}{vout}")
            filters.append(f"{last_a}[{i}:a]acrossfade=d={transition}{aout}")
            last_v, last_a = vout, aout
            offset += probe_duration(parts[i])
        run_ff(["ffmpeg", "-y", "-loglevel", "error", *inputs,
                "-filter_complex", ";".join(filters),
                "-map", last_v, "-map", last_a,
                "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(output)])
    else:
        listing = output.parent / f"{output.stem}_concat.txt"
        listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
        run_ff(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-i", str(listing), "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(output)])
        listing.unlink(missing_ok=True)

    if audio == "mute":
        tmp = output.with_name(f"{output.stem}_muted.mp4")
        run_ff(["ffmpeg", "-y", "-loglevel", "error", "-i", str(output),
                "-an", "-c:v", "copy", "-movflags", "+faststart", str(tmp)])
        tmp.replace(output)
    elif audio == "replace":
        if not external_audio or not Path(external_audio).is_file():
            raise H3Error("--audio replace 需要用 --external-audio 指定音频文件")
        tmp = output.with_name(f"{output.stem}_dub.mp4")
        run_ff(["ffmpeg", "-y", "-loglevel", "error", "-i", str(output),
                "-i", str(external_audio), "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                "-movflags", "+faststart", str(tmp)])
        tmp.replace(output)


# --------------------------------------------------------------------------- 链式生成


def build_content(prompt: str, *, mode: str, first_frame: str | None = None,
                  references: list[str] | None = None,
                  reference_audio: str | None = None) -> list[dict]:
    """组装 content 数组。

    H3 硬约束：first_frame/last_frame 与 reference_* 互斥，二者不可混用。
    """
    content: list[dict] = [{"type": "text", "text": prompt}]

    if mode == "frame":
        if first_frame:
            content.append({"type": "image_url", "image_url": {"url": image_ref(first_frame)},
                            "role": "first_frame"})
    elif mode == "reference":
        refs = references or []
        if len(refs) > MAX_REFERENCE_IMAGES:
            raise H3Error(f"参考图 {len(refs)} 张，超过上限 {MAX_REFERENCE_IMAGES}")
        for ref in refs:
            content.append({"type": "image_url", "image_url": {"url": image_ref(ref)},
                            "role": "reference_image"})
        if reference_audio:
            content.append({"type": "audio_url", "audio_url": {"url": image_ref(reference_audio)},
                            "role": "reference_audio"})
    else:
        raise H3Error(f"未知串接模式 {mode!r}（可用 frame / reference）")
    return content


def run_chain(args: argparse.Namespace) -> None:
    segments_spec = load_storyboard(args.storyboard)
    durations = [s["duration"] or args.prefer_duration for s in segments_spec]
    for d in durations:
        if not MIN_SEG <= d <= MAX_SEG:
            raise H3Error(f"段时长 {d}s 超出 {MIN_SEG}~{MAX_SEG}s")

    workdir = Path(args.workdir)
    parts_dir = workdir / "parts"
    frames_dir = workdir / "frames"
    state_file = workdir / "state.json"
    for d in (parts_dir, frames_dir):
        d.mkdir(parents=True, exist_ok=True)

    base_refs = [args.image] if args.image else []
    if args.extra_reference:
        base_refs += args.extra_reference
    images_per_call = (1 if args.mode == "frame" else len(base_refs) + (1 if args.carry_frame else 0))

    cost = estimate_cost(durations, args.resolution, images_per_call, args.regenerate_2k)
    print(f"分镜 {len(durations)} 段 → {cost['seconds']}s: {durations}")
    print(f"串接模式: {args.mode}   分辨率: {args.resolution}   转场: {args.transition}s")
    print(f"成本估算: 视频 {cost['video']} 元" +
          (f" + 图片 {cost['images']} 元" if cost["images"] else "") +
          (f" + 2K再生成 {cost['regenerate']} 元" if cost["regenerate"] else "") +
          f" ≈ **{cost['total']} 元**")
    if args.transition > 0:
        lost = args.transition * (len(durations) - 1)
        print(f"注意：{len(durations) - 1} 处转场会吃掉约 {lost:.1f}s，成片约 "
              f"{cost['seconds'] - lost:.1f}s（想要足 {args.total or cost['seconds']}s 请多留一段）")

    if args.mode == "frame" and base_refs:
        print("提示：frame 模式下参考图只用作第 1 段首帧；H3 不允许同时传 reference_image")

    if args.dry_run:
        print("\n--- dry-run：以下是将要发出的请求（不会调用 API、不产生费用）---")
        for i, (spec, dur) in enumerate(zip(segments_spec, durations), 1):
            first = base_refs[0] if (i == 1 and base_refs) else f"<第{i-1}段末帧>"
            refs = base_refs + ([f"<第{i-1}段末帧>"] if args.carry_frame and i > 1 else [])
            print(f"\n[段 {i}/{len(durations)}] {dur}s  {spec['title'] or ''}")
            print(f"  prompt: {spec['prompt'][:110]}{'…' if len(spec['prompt']) > 110 else ''}")
            if args.mode == "frame":
                print(f"  first_frame: {first}")
            else:
                print(f"  reference_image: {refs}")
                if args.reference_audio:
                    print(f"  reference_audio: {args.reference_audio}")
        print("\n去掉 --dry-run 即开始真实生成。建议先跑 2 段小样确认衔接效果。")
        return

    key = api_key()
    state = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except ValueError:
            state = {}

    parts: list[Path] = []
    prev_frame: Path | None = None

    for i, (spec, dur) in enumerate(zip(segments_spec, durations), 1):
        part_path = parts_dir / f"seg{i:02d}.mp4"
        frame_path = frames_dir / f"seg{i:02d}_last.jpg"
        done = state.get(str(i), {})

        if part_path.is_file() and done.get("completed"):
            print(f"[段 {i}/{len(durations)}] 已完成，跳过（删除 {part_path} 可重跑）")
            parts.append(part_path)
            prev_frame = frame_path if frame_path.is_file() else extract_last_frame(part_path, frame_path)
            continue

        print(f"\n[段 {i}/{len(durations)}] {dur}s  {spec['title'] or ''}")
        if args.mode == "frame":
            first_frame = base_refs[0] if i == 1 and base_refs else (str(prev_frame) if prev_frame else None)
            if i > 1 and not first_frame:
                raise H3Error("frame 模式缺少上一段末帧，无法接续")
            content = build_content(spec["prompt"], mode="frame", first_frame=first_frame)
            ratio = args.ratio if (i == 1 and not first_frame) else None
        else:
            refs = list(base_refs)
            if args.carry_frame and prev_frame:
                refs.append(str(prev_frame))
            if not refs:
                raise H3Error("reference 模式需要至少一张参考图（--image）")
            content = build_content(spec["prompt"], mode="reference", references=refs,
                                    reference_audio=args.reference_audio)
            ratio = args.ratio

        task_id = done.get("task_id")
        if not task_id:
            task_id = create_task(content, dur, args.resolution, ratio, key)
            state[str(i)] = {"task_id": task_id, "completed": False}
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  task_id={task_id}")
        else:
            print(f"  复用已提交的 task_id={task_id}")

        url = wait_task(task_id, key, args.poll_interval, args.timeout)
        download(url, part_path)
        print(f"  已下载 {part_path.name}（{part_path.stat().st_size / 1048576:.1f}MB，"
              f"实际 {probe_duration(part_path):.1f}s）")

        state[str(i)] = {"task_id": task_id, "completed": True}
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        parts.append(part_path)
        prev_frame = extract_last_frame(part_path, frame_path)

    print(f"\n拼接 {len(parts)} 段 → {args.output}")
    concat_videos(parts, Path(args.output), args.transition, args.audio, args.external_audio)
    final = Path(args.output)
    print(f"完成：{final}（{final.stat().st_size / 1048576:.1f}MB，{probe_duration(final):.1f}s）")
    if args.audio == "keep":
        print("提醒：每段音频都是 H3 独立生成的，段间音色/环境音可能不连续。"
              "口播成片建议 --audio replace 换成你自己的配音，再做一次对口型。")


# --------------------------------------------------------------------------- 子命令


def cmd_plan(args: argparse.Namespace) -> None:
    if args.storyboard:
        specs = load_storyboard(args.storyboard)
        durations = [s["duration"] or args.prefer_duration for s in specs]
        print(f"分镜脚本 {args.storyboard}：{len(specs)} 段，合计 {sum(durations)}s")
        for i, (s, d) in enumerate(zip(specs, durations), 1):
            print(f"  段 {i:>2d}  {d:>2d}s  {s['title'] or '(无标题)'}")
            print(f"        {s['prompt'][:96]}{'…' if len(s['prompt']) > 96 else ''}")
    else:
        durations = plan_segments(args.total, args.prefer_duration)
        print(f"总时长 {args.total}s → {len(durations)} 段: {durations}")

    for res in ("768P", "2K"):
        c = estimate_cost(durations, res, 1)
        print(f"  {res}: {c['seconds']}s × {PRICE_PER_SECOND[res]} 元/s ≈ {c['total']} 元")
    c2 = estimate_cost(durations, "768P", 1, regenerate_2k=True)
    print(f"  768P 生成后再生成 2K ≈ {c2['total']} 元")


def load_prompt_text(path: str) -> str:
    """读单段提示词文件。以 # 开头的行视为注释。"""
    p = Path(path)
    if not p.is_file():
        raise H3Error(f"找不到提示词文件 {path}")
    lines = [line.rstrip() for line in p.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("#")]
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if not text:
        raise H3Error(f"提示词文件为空（或全是注释）: {path}")
    return text


def cmd_single(args: argparse.Namespace) -> None:
    if args.prompt and args.prompt_file:
        raise H3Error("--prompt 与 --prompt-file 只能二选一")
    prompt = load_prompt_text(args.prompt_file) if args.prompt_file else args.prompt
    if not prompt:
        raise H3Error("需要 --prompt 或 --prompt-file（推荐 prompts/h3_electricity_safety_single.txt）")

    if args.image and args.reference:
        raise H3Error("--image（首帧）与 --reference（参考图）互斥，H3 不允许混用")

    if args.reference:
        content = build_content(prompt, mode="reference", references=args.reference,
                                reference_audio=args.reference_audio)
        ratio = args.ratio
    else:
        content = build_content(prompt, mode="frame", first_frame=args.image)
        ratio = args.ratio if not args.image else None
    if args.last_frame:
        content.append({"type": "image_url", "image_url": {"url": image_ref(args.last_frame)},
                        "role": "last_frame"})

    cost = estimate_cost([args.duration], args.resolution,
                         len(args.reference or []) or (1 if args.image else 0))
    print(f"单段 {args.duration}s @ {args.resolution} ≈ {cost['total']} 元")
    if args.dry_run:
        preview = [{**c, "image_url": {"url": "<data-uri 已省略>"}} if c["type"] == "image_url" else c
                   for c in content]
        print(json.dumps({"model": MODEL, "content": preview, "duration": args.duration,
                          "resolution": args.resolution, "ratio": ratio},
                         ensure_ascii=False, indent=2))
        print("dry-run 结束，未调用 API。")
        return

    key = api_key()
    task_id = create_task(content, args.duration, args.resolution, ratio, key)
    print(f"task_id={task_id}")
    url = wait_task(task_id, key, args.poll_interval, args.timeout)
    out = download(url, Path(args.output))
    print(f"完成：{out}（{out.stat().st_size / 1048576:.1f}MB，{probe_duration(out):.1f}s）")


def main() -> None:
    parser = H3ArgumentParser(
        description="MiniMax H3 长视频串接：用 4~15 秒分段拼出 70 秒级成片",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="只做规划与成本估算，不调 API（免费）")
    p_plan.add_argument("--storyboard",
                        help="分镜脚本，推荐 prompts/h3_electricity_safety_70s.txt")
    p_plan.add_argument("--total", type=int, default=70, help="没有分镜时按总时长自动分段")
    p_plan.add_argument("--prefer-duration", type=int, default=14, help="每段目标时长（4~15）")
    p_plan.set_defaults(func=cmd_plan)

    p_single = sub.add_parser("single", help="生成单段（最便宜的连通性验证）")
    p_single.add_argument("--prompt", default="")
    p_single.add_argument("--prompt-file",
                          help="提示词文件，推荐 prompts/h3_electricity_safety_single.txt")
    p_single.add_argument("--image", help="首帧图（本地文件或 URL）")
    p_single.add_argument("--last-frame", help="尾帧图，需与 --image 搭配")
    p_single.add_argument("--reference", action="append",
                         help="参考图，可重复；与 --image 互斥（H3 硬约束）")
    p_single.add_argument("--reference-audio", help="参考音频（音色参考，≤15s）")
    p_single.add_argument("--duration", type=int, default=4, help=f"{MIN_SEG}~{MAX_SEG} 整数秒")
    p_single.add_argument("--resolution", choices=["768P", "2K"], default="768P")
    p_single.add_argument("--ratio", default="16:9",
                         help="文生视频必填且不能 adaptive；给了首帧则被忽略")
    add_output_arg(p_single, "h3_single.mp4")
    p_single.add_argument("--poll-interval", type=int, default=10)
    p_single.add_argument("--timeout", type=int, default=1800)
    p_single.add_argument("--dry-run", action="store_true")
    p_single.set_defaults(func=cmd_single)

    p_chain = sub.add_parser("chain", help="按分镜串接多段成长视频")
    p_chain.add_argument("--storyboard", required=True,
                        help="分镜脚本，推荐 prompts/h3_electricity_safety_probe.txt 或 _70s.txt")
    p_chain.add_argument("--mode", choices=["frame", "reference"], default="frame",
                        help="frame=末帧接首帧(画面连贯) / reference=统一参考图(身份稳)")
    p_chain.add_argument("--image", help="角色图：frame 模式作第 1 段首帧；reference 模式作参考图")
    p_chain.add_argument("--extra-reference", action="append", help="额外参考图（仅 reference 模式）")
    p_chain.add_argument("--reference-audio", help="参考音频，统一音色（仅 reference 模式，≤15s）")
    p_chain.add_argument("--carry-frame", action="store_true",
                        help="reference 模式下把上一段末帧也加进参考图（兼顾连贯）")
    p_chain.add_argument("--prefer-duration", type=int, default=14, help="分镜未写时长时用它")
    p_chain.add_argument("--total", type=int, help="仅用于提示成片时长差异")
    p_chain.add_argument("--resolution", choices=["768P", "2K"], default="768P")
    p_chain.add_argument("--ratio", default="16:9")
    p_chain.add_argument("--transition", type=float, default=0.0,
                        help="段间交叉溶解秒数，掩盖边界跳变（reference 模式建议 0.3~0.5）")
    p_chain.add_argument("--audio", choices=["keep", "mute", "replace"], default="keep",
                        help="keep=保留 H3 原声 / mute=静音 / replace=换成 --external-audio")
    p_chain.add_argument("--external-audio", help="配 --audio replace 使用")
    p_chain.add_argument("--regenerate-2k", action="store_true", help="仅用于成本估算提示")
    p_chain.add_argument("--workdir", default="data/h3_chain", help="分段与断点续跑状态目录")
    add_output_arg(p_chain, "h3_long.mp4")
    p_chain.add_argument("--poll-interval", type=int, default=10)
    p_chain.add_argument("--timeout", type=int, default=3600)
    p_chain.add_argument("--dry-run", action="store_true", help="只预演，不调 API、不花钱")
    p_chain.set_defaults(func=run_chain)

    args = parser.parse_args()
    load_dotenv()
    try:
        args.func(args)
    except H3Error as exc:
        sys.exit(f"错误：{exc}")
    except KeyboardInterrupt:
        sys.exit("\n已中断。已完成的分段保留在 workdir，重跑会自动续上。")


if __name__ == "__main__":
    main()
