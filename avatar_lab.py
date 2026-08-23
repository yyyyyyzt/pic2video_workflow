#!/usr/bin/env python3
"""数字人方案实验台：同一张图 + 同一段音频，横向跑多个方案并给出可比对的结论。

它解决的不是「生成一条视频」，而是「在十几个候选模型里挑出哪个能过平台入库」。
所以每个方案跑完都会自动走完整条流水线：

    上传素材 → 生成 → （可选）超分到 1080p → 规范化(比例/帧率/容器) → 入库合规校验

10 秒筛选不要一条条改参数。用参数矩阵一次跑完所有组合：

    python3 avatar_lab.py sweep --image face.jpg

默认矩阵 screen10 = 3 个模型 × 3 档静默 × 2 条提示词。开头静默靠音频，
不要用静止帧替换（会不眨眼）。看完 data/lab/sweep-screen10/index.html 对照即可。

最后汇总成一张表，用客观数字排序，而不是靠肉眼感觉。

常用命令：
    python3 avatar_lab.py balance
    python3 tts.py --list-voices
    python3 tts.py --script-file prompts/tencent_general_script_10s.txt -o speech_10s.mp3 --silence 2
    python3 avatar_lab.py sweep --image face.jpg
    python3 avatar_lab.py run --recipes omnihuman-15 --image face.jpg \\
        --script-file prompts/tencent_general_script_60s.txt
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import wsclient
from recipes import GROUPS, RECIPES, ROUTE_LABELS, Recipe, build_payload, resolve
from lab.report import defect_lines, summarize, write_sweep_index
from lab.table import render_table
from wsclient import load_dotenv

DEFAULT_OUTDIR = Path("data/lab")
DEFAULT_PROMPT_FILE = Path("prompts/tencent_general_prompt.txt")
DEFAULT_SCRIPT_10S = Path("prompts/tencent_general_script_10s.txt")

# 各档位的时长下限，用来在合成台词后立刻提醒，而不是等生成完才发现太短
PROFILE_MIN_SECONDS = {
    "tencent-general": 60, "tencent-broadcast": 30, "tencent-hifi": 120,
    "aliyun-fewshot": 10, "aliyun-hifi": 270,
}


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# list / plan
# ---------------------------------------------------------------------------

def _resolve_upscaler(name: str | None) -> Recipe | None:
    """把 --upscale 的取值解析成 Recipe。空串 / none / off 都表示不超分。"""
    if not name or str(name).strip().lower() in ("none", "off", "no"):
        return None
    key = str(name).strip()
    if key not in RECIPES:
        raise KeyError(f"未知超分方案 {key!r}，可选：{', '.join(r for r in RECIPES if r.startswith('up-'))}")
    return RECIPES[key]


def _cost_of(recipe: Recipe, seconds: float) -> tuple[float, bool]:
    """返回 (成本, 是否为实测值)。

    单价来源优先级：本地校准表（你自己跑出来的）> 代码里已验证的值 > 公式粗估。
    """
    measured = wsclient.measured_per_second(recipe.model)
    known = measured or recipe.verified_per_second
    return recipe.price_for(seconds, measured), known is not None


def cmd_list(args: argparse.Namespace) -> int:
    seconds = args.seconds
    for route, label in ROUTE_LABELS.items():
        items = [r for r in RECIPES.values() if r.route == route]
        if not items:
            continue
        print(f"\n=== 路线 {route} · {label} ===")
        rows = []
        for r in items:
            cost, real = _cost_of(r, seconds)
            rows.append([
                r.key, r.label,
                "+".join(r.needs),
                r.resolution or ("1080p" if r.native_1080p else "-"),
                f"${cost:.2f}" + ("" if real else "?"),
                r.note,
            ])
        print(render_table(["方案", "说明", "输入", "分辨率", f"{seconds:g}s 成本", "备注"], rows))
    print(f"\n组合：screen = {' + '.join(GROUPS['screen'])}（10 秒筛选）")
    print(f"      tencent = {' + '.join(GROUPS['tencent'])}（60 秒质量档）")
    print(f"\n成本按 {seconds:g} 秒计。带 ? 的是粗估：WaveSpeed 的 base_price 单位不统一"
          "（有的每秒、有的每 5 秒），官方也声明以实际扣费为准。")
    print("跑过一次之后会用实测单价（data/cost_calibration.json），? 消失即为实测。")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    picked = resolve(args.recipes)
    seconds = args.seconds
    upscale = _resolve_upscaler(args.upscale)

    rows, total, any_guess = [], 0.0, False
    for r in picked:
        gen, gen_real = _cost_of(r, seconds)
        up, up_real = (0.0, True)
        if upscale and not r.native_1080p and r.route != "U":
            up, up_real = _cost_of(upscale, seconds)
        any_guess |= not (gen_real and up_real)
        mark = "" if (gen_real and up_real) else "?"
        rows.append([r.key, r.label, f"${gen:.2f}",
                     f"${up:.2f}" if up else "-", f"${gen + up:.2f}{mark}"])
        total += gen + up

    print(render_table(["方案", "说明", "生成", "超分", "小计"], rows))
    print(f"\n合计 ${total:.2f}{'（带 ? 的为粗估，可能偏差数倍）' if any_guess else '（全部为实测单价）'}"
          f"　{len(picked)} 个方案 × {seconds:g} 秒")

    try:
        bal = wsclient.balance()
        print(f"当前余额 ${bal:.2f}", end="")
        print(" — 余额不足，请缩短 --seconds 或减少方案" if total > bal else " — 够跑")
    except wsclient.WaveSpeedError as exc:
        print(f"（余额查询失败：{exc}）")
    return 0


def cmd_balance(_: argparse.Namespace) -> int:
    print(f"WaveSpeed 余额：${wsclient.balance():.4f}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    models = wsclient.list_models(refresh=args.refresh)
    kw = (args.filter or "").lower()
    hits = [m for m in models
            if kw in m["model_id"].lower() or kw in (m.get("type") or "").lower()]
    rows = [[m["model_id"], m.get("type", ""), f"${m.get('base_price')}"]
            for m in sorted(hits, key=lambda x: x["model_id"])]
    print(render_table(["model_id", "type", "base_price/5s"], rows))
    print(f"\n共 {len(hits)} / {len(models)} 个模型")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """花钱之前先卡输入。图片比例不对或音频太短，生成出来必然不达标。"""
    from compliance import PROFILES, ComplianceError, probe

    profile = PROFILES[args.profile]
    print(f"目标档位：{profile.label}")
    print(f"  时长 {profile.min_seconds:g}~{profile.max_seconds:g}s"
          f"　短边 ≥{profile.min_height}　{profile.min_fps:g}~{profile.max_fps:g}fps"
          f"　{' 或 '.join(profile.allowed_ratios)}")
    print()

    problems: list[str] = []

    if args.image:
        try:
            import struct

            path = Path(args.image)
            w = h = 0
            # 只读文件头拿尺寸，不引入 Pillow 依赖
            data = path.read_bytes()
            if data[:2] == b"\xff\xd8":                       # JPEG
                i = 2
                while i < len(data) - 9:
                    if data[i] != 0xFF:
                        i += 1
                        continue
                    marker = data[i + 1]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                        h, w = struct.unpack(">HH", data[i + 5:i + 9])
                        break
                    i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
            elif data[:8] == b"\x89PNG\r\n\x1a\n":            # PNG
                w, h = struct.unpack(">II", data[16:24])

            if w and h:
                ratio = w / h
                print(f"角色图 {path.name}：{w}×{h}，比例 {ratio:.3f}")
                short = min(w, h)
                if short < 720:
                    problems.append(
                        f"图片短边只有 {short}px，偏小。生成模型不会凭空补细节，"
                        f"超分也救不回来，建议换一张短边 ≥1080 的图")
                want_portrait = "9:16" in profile.allowed_ratios
                if want_portrait and abs(ratio - 9 / 16) > 0.15 and abs(ratio - 16 / 9) > 0.15:
                    problems.append(
                        f"图片比例 {ratio:.2f} 既不接近 16:9 也不接近 9:16，"
                        f"成片补边后人物会偏小，建议先按目标比例裁好再送进来")
            else:
                print(f"角色图 {path.name}：读不出尺寸（只支持 jpg/png 头解析），跳过检查")
        except (OSError, struct.error, IndexError) as exc:
            problems.append(f"角色图读取失败：{exc}")

    if args.audio:
        from mediaprep import audio_duration

        dur = audio_duration(args.audio)
        total = dur + args.silence
        print(f"驱动音频 {Path(args.audio).name}：{dur:.1f}s"
              f"（加 {args.silence:g}s 静默头后 {total:.1f}s）")
        if total < profile.min_seconds:
            import tts
            problems.append(
                f"成片会是 {total:.1f}s，短于该档位要求的 {profile.min_seconds:g}s。"
                f"台词还需要再加约 {int((profile.min_seconds - total) * tts.CHARS_PER_SECOND)} 字")
        if total > profile.max_seconds:
            problems.append(f"成片会是 {total:.1f}s，超过上限 {profile.max_seconds:g}s")

    if args.video:
        info = probe(args.video)
        print(f"模板视频 {Path(args.video).name}："
              f"{info['width']}×{info['height']} {info['fps']:g}fps {info['duration']:.1f}s")
        if info["duration"] > 120:
            problems.append(
                f"模板视频 {info['duration']:.0f}s，Wan2.2-Animate 类模型标称上限 120s，"
                f"超出部分可能被截断")

    print()
    if problems:
        print("发现问题：")
        for p in problems:
            print(f"  - {p}")
        print("\n这些问题会让成片无法入库，建议先修再花钱生成。")
        return 1
    print("输入检查通过，可以跑 run 了。")
    return 0


def cmd_tts(args: argparse.Namespace) -> int:
    import tts
    from mediaprep import audio_duration, prepend_silence

    text = args.text or tts.read_script(args.script_file)
    est = tts.estimate_seconds(text, args.speed)
    print(f"台词 {len(text)} 字，预计约 {est:.0f} 秒，音色 {args.voice}")
    out, cost = tts.synthesize(text, args.output, voice=args.voice, speed=args.speed,
                               emotion=args.emotion,
                               on_tick=lambda s, e: _log(f"  {s} ({e}s)"))
    if args.silence > 0:
        raw = out.with_name(out.stem + "_raw" + out.suffix)
        out.rename(raw)
        out = prepend_silence(raw, Path(args.output).with_suffix(".mp3"), args.silence)
        print(f"已在开头接 {args.silence:g} 秒静音（原始合成保留为 {raw.name}）")
    print(f"已生成 {out}，实际 {audio_duration(out):.1f}s，扣费 ${cost:.4f}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    result = wsclient.poll(args.prediction_id, timeout=args.timeout, interval=args.interval,
                           on_tick=lambda s, e: _log(f"  {s} ({e}s)"))
    out = Path(args.output or f"data/lab/{args.prediction_id}.mp4")
    wsclient.download(result["outputs"][0], out)
    print(f"已下载 {out}")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _read_prompt(args: argparse.Namespace) -> str:
    prompt_file = args.prompt_file
    if not args.prompt and not prompt_file and DEFAULT_PROMPT_FILE.is_file():
        prompt_file = str(DEFAULT_PROMPT_FILE)
    if prompt_file:
        lines = Path(prompt_file).read_text(encoding="utf-8").splitlines()
        return " ".join(l.strip() for l in lines
                        if l.strip() and not l.strip().startswith("#"))
    return args.prompt or ""


def _evaluate(video: Path, *, silence: float, reference_image=None) -> dict:
    """跑客观指标。缺依赖时返回 available=False，并把原因写进日志——
    以前这里静默返回空字典，结果指标没跑也看不出来。"""
    try:
        import videometrics
    except ImportError as exc:
        return {"available": False, "note": f"缺依赖：{exc}"}
    result = videometrics.evaluate_to_dict(
        video, silence_seconds=silence, reference_image=reference_image)
    if not result.get("available"):
        _log(f"客观指标跳过：{result.get('note')}")
    return result


def _log_compliance(recipe_key: str, report, profile: str) -> None:
    """把入库校验的每一项打到实时日志，避免只看到「不通过」却不知道挂在哪。"""
    _log(f"[{recipe_key}] 入库校验 {'通过' if report.passed else '不通过'}")
    if report.passed:
        return
    failed = [c for c in report.checks if not c.ok]
    for c in failed:
        _log(f"[{recipe_key}]   [!!] {c.name}: {c.detail}")
    names = [c.name for c in failed]
    duration = report.probe.get("duration", 0)
    if names == ["时长"] and duration < 60 and profile == "tencent-general":
        _log(f"[{recipe_key}]   这是 10s 筛选轮次的预期结果，换 60s 台词再交平台")


def _billed(fn, model_id: str, seconds: float) -> tuple[dict, float]:
    """执行一次计费调用，用余额差额测出真实成本并写进校准表。

    WaveSpeed 的 base_price 单位不统一，任务详情里也不返回扣费金额，
    所以只能前后各查一次余额来测。查余额本身不花钱。
    """
    try:
        before = wsclient.balance()
    except wsclient.WaveSpeedError:
        before = None

    result = fn()

    cost = 0.0
    if before is not None:
        try:
            cost = round(before - wsclient.balance(), 4)
        except wsclient.WaveSpeedError:
            cost = 0.0
    if cost > 0:
        wsclient.record_calibration(model_id, cost, seconds)
    return result, cost


def _run_one(recipe: Recipe, urls: dict, ctx: dict) -> dict:
    """跑单个方案的完整流水线，返回结果记录。"""
    from compliance import ComplianceError, check, probe
    from mediaprep import normalize, replace_silent_head, snapshot_frames

    cell_id = ctx.get("cell_id") or recipe.key
    log_key = ctx.get("log_key") or recipe.key
    workdir: Path = ctx["outdir"] / cell_id
    workdir.mkdir(parents=True, exist_ok=True)
    record: dict = {"recipe": recipe.key, "label": recipe.label, "route": recipe.route,
                    "model": recipe.model, "cell_id": cell_id,
                    "started_at": datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()

    try:
        payload = build_payload(
            recipe,
            image_url=urls.get("image", ""), audio_url=urls.get("audio", ""),
            video_url=urls.get("video", ""), prompt=ctx["prompt"], seed=ctx["seed"],
        )
        _log(f"[{log_key}] 提交 {recipe.model}")
        billable = recipe.billable_seconds(ctx["seconds"])
        result, gen_cost = _billed(
            lambda: wsclient.run(recipe.model, payload, timeout=ctx["timeout"],
                                 interval=ctx["interval"],
                                 on_tick=lambda s, e: _log(f"[{log_key}]   {s} ({e}s)")),
            recipe.model, billable)
        record["prediction_id"] = result["prediction_id"]
        record["cost_generate"] = gen_cost

        raw = wsclient.download(result["outputs"][0], workdir / "01_raw.mp4")
        record["raw"] = str(raw)
        record["raw_probe"] = probe(raw)
        _log(f"[{log_key}] 生成完成 {record['raw_probe']['width']}×"
             f"{record['raw_probe']['height']} {record['raw_probe']['duration']:.1f}s"
             f"，实际扣费 ${gen_cost:.4f}")

        current = raw
        up_cost = 0.0

        # 超分：原生 1080p 的方案跳过，避免白花钱和二次劣化
        upscaler: Recipe | None = ctx["upscaler"]
        if upscaler and not recipe.native_1080p and recipe.route != "U":
            _log(f"[{log_key}] 超分 {upscaler.model}")
            up_url = wsclient.upload(current)
            up_payload = dict(upscaler.params, video=up_url)
            up, up_cost = _billed(
                lambda: wsclient.run(upscaler.model, up_payload, timeout=ctx["timeout"],
                                     interval=ctx["interval"]),
                upscaler.model, record["raw_probe"]["duration"])
            current = wsclient.download(up["outputs"][0], workdir / "02_upscaled.mp4")
            record["upscaled"] = str(current)
            record["cost_upscale"] = up_cost

        # 规范化到平台档位。静止帧替换默认关闭：会不眨眼、开头很呆。
        silence = float(ctx.get("silence") or 0)
        image_local = ctx.get("image_local")
        still_head = bool(ctx.get("still_head")) and silence > 0
        if still_head and image_local and Path(image_local).is_file():
            current = normalize(current, workdir / "03_normalized.mp4", profile=ctx["profile"])
            _log(f"[{log_key}] 静帧替换开头 {silence:g}s（显式开启，画面会冻住）")
            final = replace_silent_head(
                current, image_local, workdir / "03_final.mp4", silence)
            record["normalized"] = str(current)
            record["still_head"] = True
        else:
            final = normalize(current, workdir / "03_final.mp4", profile=ctx["profile"])
            record["still_head"] = False
        record["final"] = str(final)
        record["final_probe"] = probe(final)

        try:
            thumbs = snapshot_frames(final, workdir / "thumbs")
            record["thumbs"] = [str(p) for p in thumbs]
        except ComplianceError as exc:
            _log(f"[{log_key}] 抽帧失败：{exc}")

        report = check(final, ctx["profile"], silence_seconds=silence or None)
        record["compliance_passed"] = report.passed
        record["compliance"] = [{"name": c.name, "ok": c.ok, "detail": c.detail}
                                for c in report.checks]
        _log_compliance(log_key, report, ctx["profile"])

        if ctx["analyze"]:
            metrics = _evaluate(final, silence=silence, reference_image=image_local)
            record["metrics"] = metrics
            if metrics.get("available"):
                _log(f"[{log_key}] 客观指标 缺陷 {metrics['major_count']} 重 / "
                     f"{metrics['minor_count']} 轻")
                for d in metrics.get("defects", []):
                    at = f" @{d['at_seconds']:g}s" if d.get("at_seconds") is not None else ""
                    _log(f"[{log_key}]   ({d['severity']}) "
                         f"{d['dimension']}/{d['code']}{at} {d['detail']}")

        record["status"] = "ok"
        record["cost_actual"] = round(gen_cost + up_cost, 4)

    except (wsclient.WaveSpeedError, ComplianceError, ValueError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        _log(f"[{log_key}] 失败：{exc}")

    record["elapsed_seconds"] = round(time.time() - t0, 1)
    (workdir / "record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def cmd_run(args: argparse.Namespace) -> int:
    from mediaprep import audio_duration, prepend_silence

    picked = resolve(args.recipes)
    if not picked:
        print("没有选中任何方案")
        return 1
    if not (args.audio or args.script or args.script_file or args.video):
        print("错误：请给出 --audio，或 --script / --script-file 走 TTS。"
              f"10 秒筛选台词：--script-file {DEFAULT_SCRIPT_10S}")
        return 1

    outdir = Path(args.outdir or DEFAULT_OUTDIR / datetime.now().strftime("%Y%m%d-%H%M%S"))
    outdir.mkdir(parents=True, exist_ok=True)

    # 给了台词就先合成驱动音频。视频时长完全由音频决定，所以这一步定成片长度
    audio_path = args.audio
    if not audio_path and (args.script or args.script_file):
        import tts

        text = args.script or tts.read_script(args.script_file)
        est = tts.estimate_seconds(text, args.tts_speed)
        _log(f"合成台词：{len(text)} 字，预计约 {est:.0f} 秒，音色 {args.voice}")
        synth, tts_cost = tts.synthesize(
            text, outdir / "input_audio_tts.mp3", voice=args.voice,
            speed=args.tts_speed, emotion=args.tts_emotion,
            on_tick=lambda s, e: _log(f"  TTS {s} ({e}s)"))
        audio_path = str(synth)
        from mediaprep import audio_duration as _dur
        actual = _dur(synth)
        _log(f"TTS 完成 {actual:.1f}s，扣费 ${tts_cost:.4f}")

        floor = PROFILE_MIN_SECONDS.get(args.profile, 0)
        if floor and actual + args.silence < floor:
            _log(f"警告：成片将只有 {actual + args.silence:.1f}s，"
                 f"短于 {args.profile} 要求的 {floor:g}s，平台会退料。"
                 f"台词再加约 {int((floor - actual - args.silence) * tts.CHARS_PER_SECOND)} 字")

    # 音频加静默头，再据此算真实时长与成本
    if audio_path and args.silence > 0:
        padded = outdir / "input_audio_padded.mp3"
        prepend_silence(audio_path, padded, args.silence)
        _log(f"已给音频加 {args.silence:g}s 静默头 → {padded.name}"
             f"（腾讯要求开头闭口 1~3 秒）")
        audio_path = str(padded)

    seconds = args.seconds or (audio_duration(audio_path) if audio_path else 10.0)
    upscaler = _resolve_upscaler(args.upscale)

    total = 0.0
    for r in picked:
        total += _cost_of(r, seconds)[0]
        if upscaler and not r.native_1080p and r.route != "U":
            total += _cost_of(upscaler, seconds)[0]

    print(f"\n将跑 {len(picked)} 个方案 × {seconds:.1f} 秒，预估总成本 ${total:.2f}")
    print("  " + ", ".join(r.key for r in picked))
    try:
        bal = wsclient.balance()
        print(f"  当前余额 ${bal:.2f}")
        if total > bal:
            print(f"\n余额不够（差 ${total - bal:.2f}）。建议先用 10 秒短音频筛方案：")
            print(f"  python3 avatar_lab.py plan --seconds 10 --recipes {','.join(args.recipes)}")
            if not args.yes:
                return 1
    except wsclient.WaveSpeedError as exc:
        print(f"  余额查询失败：{exc}")

    if total > args.max_cost and not args.yes:
        print(f"\n预估 ${total:.2f} 超过 --max-cost ${args.max_cost:.2f}，"
              f"确认要跑就加 --yes，或降低 --seconds")
        return 1
    if not args.yes:
        try:
            if input("\n继续？[y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except EOFError:
            print("非交互环境，请显式加 --yes")
            return 1

    # 素材只上传一次，所有方案复用同一组 URL，保证横向可比
    urls: dict[str, str] = {}
    for kind, src in (("image", args.image), ("audio", audio_path), ("video", args.video)):
        if src:
            _log(f"上传{kind}：{Path(src).name if not str(src).startswith('http') else src}")
            urls[kind] = wsclient.upload(src)

    image_local = args.image if args.image and not str(args.image).startswith("http") else None
    ctx = {
        "outdir": outdir, "prompt": _read_prompt(args), "seed": args.seed,
        "timeout": args.timeout, "interval": args.interval, "profile": args.profile,
        "upscaler": upscaler, "seconds": seconds, "analyze": not args.no_analyze,
        "silence": args.silence, "still_head": args.still_head,
        "image_local": image_local,
    }

    records = []
    for recipe in picked:
        missing = [n for n in recipe.needs if not urls.get(n)]
        if missing:
            _log(f"[{recipe.key}] 跳过：缺少输入 {'/'.join(missing)}")
            records.append({"recipe": recipe.key, "label": recipe.label,
                            "status": "skipped", "error": f"缺少 {'/'.join(missing)}"})
            continue
        records.append(_run_one(recipe, urls, ctx))

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {"image": args.image, "audio": audio_path, "video": args.video,
                   "prompt": ctx["prompt"], "seconds": seconds,
                   "silence": args.silence, "still_head": args.still_head},
        "profile": args.profile, "upscale": args.upscale,
        "records": records,
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(summarize(records))
    print(f"\n结果目录：{outdir}")
    try:
        print(f"剩余余额：${wsclient.balance():.4f}")
    except wsclient.WaveSpeedError:
        pass
    return 0


def _list_matrices() -> int:
    from matrices import MATRICES, expand
    rows = []
    for mx in MATRICES.values():
        n = len(expand(mx))
        rows.append([
            mx.key, mx.label, str(n),
            ",".join(mx.recipes),
            ",".join(f"{s:g}s" for s in mx.silences),
            ",".join(mx.prompts),
        ])
    print(render_table(["矩阵", "说明", "格子", "模型", "静默", "提示词"], rows))
    print("\n一条命令跑默认矩阵（10 秒完整对比）：")
    print("  python3 avatar_lab.py sweep --image face.jpg")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """按写死的参数矩阵跑完所有组合，产出对照页。"""
    from mediaprep import audio_duration, prepend_silence
    from matrices import expand, read_prompt, recipes_of, resolve_matrix
    import tts

    if args.list_matrices:
        return _list_matrices()

    mx = resolve_matrix(args.matrix)
    all_cells = expand(mx)
    cells = expand(mx, args.only)
    if not cells:
        print(f"--only {args.only!r} 没匹配到任何格子。全部格子：")
        print("  " + ", ".join(c.id for c in all_cells))
        return 1
    picked = recipes_of(mx)
    by_key = {r.key: r for r in picked}

    def all_records(known: dict) -> list[dict]:
        """汇总和对照页始终按完整矩阵铺开，这样分批跑也能看到全貌。"""
        return [known.get(c.id, {"cell_id": c.id, "status": "pending"})
                for c in all_cells]

    # 成本必须按台词稿的真实长度估。写死一个秒数会在入库轮（80 秒）
    # 少报好几倍，而这个数直接决定你要不要充钱。
    script_text = tts.read_script(mx.script_file)
    speech_seconds = tts.estimate_seconds(script_text, mx.tts_speed)
    upscaler = _resolve_upscaler(mx.upscale)

    def cell_cost(cell) -> float:
        recipe = by_key[cell.recipe]
        seconds = speech_seconds + cell.silence
        total = _cost_of(recipe, seconds)[0]
        if upscaler and not recipe.native_1080p and recipe.route != "U":
            total += _cost_of(upscaler, seconds)[0]
        return total

    est = sum(cell_cost(c) for c in cells)
    est_all = sum(cell_cost(c) for c in all_cells)

    print(f"矩阵 {mx.key}：{mx.label}")
    print(f"  {mx.note}")
    print(f"  {len(cells)} 个格子"
          + (f"（已用 --only 从 {len(all_cells)} 个里筛出）" if args.only else
             f" = {len(mx.recipes)} 模型 × {len(mx.silences)} 静默 × {len(mx.prompts)} 提示词"))
    print(f"  台词 {mx.script_file}：{len(script_text)} 字，预计口播 {speech_seconds:.0f}s")
    print(f"  音色 {mx.voice}　超分 {mx.upscale or '关'}　静止帧替换 关")
    print(f"  预估成本 ${est:.2f}（含超分，不含 TTS）"
          + (f"，整个矩阵 ${est_all:.2f}" if args.only else ""))
    rows = [[f"{c.index:02d}", c.id, c.recipe, f"{c.silence:g}s", c.prompt_key,
             f"{speech_seconds + c.silence:.0f}s", f"${cell_cost(c):.2f}"]
            for c in cells]
    print()
    print(render_table(["#", "格子", "模型", "静默", "提示词", "成片", "估成本"], rows))

    if args.dry_run:
        outdir = Path(args.outdir or DEFAULT_OUTDIR / f"sweep-{mx.key}")
        print(f"\n--dry-run，实际会写到 {outdir}")
        return 0

    if not args.image:
        print("错误：sweep 需要 --image（角色图本地路径）")
        print("  python3 avatar_lab.py sweep --image face.jpg")
        return 1
    image_path = Path(args.image)
    if not str(args.image).startswith("http") and not image_path.is_file():
        print(f"错误：找不到角色图 {args.image}")
        return 1

    outdir = Path(args.outdir or DEFAULT_OUTDIR / f"sweep-{mx.key}")
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        bal = wsclient.balance()
        print(f"\n当前余额 ${bal:.2f}")
        if est > bal and not args.yes:
            print(f"预估 ${est:.2f} 超过余额 ${bal:.2f}。两个选择：")
            cheapest = min(cells, key=cell_cost)
            print(f"  1) 先跑最便宜的一个：--only {cheapest.recipe}"
                  f"（约 ${cell_cost(cheapest):.2f}）")
            print(f"  2) 充值后原命令续跑，已完成的格子会自动跳过")
            return 1
    except wsclient.WaveSpeedError as exc:
        print(f"  余额查询失败：{exc}")

    # TTS 只合成一次，再按静默档位垫音频
    audio_dir = outdir / "audio"
    audio_dir.mkdir(exist_ok=True)
    existing_tts = sorted(audio_dir.glob("tts_raw.*"))
    if existing_tts:
        raw_audio = existing_tts[0]
        _log(f"复用已有 TTS {raw_audio}")
    else:
        text = tts.read_script(mx.script_file)
        est_s = tts.estimate_seconds(text, mx.tts_speed)
        _log(f"合成台词：{len(text)} 字，预计约 {est_s:.0f} 秒，音色 {mx.voice}")
        synth, tts_cost = tts.synthesize(
            text, audio_dir / "tts_raw.mp3", voice=mx.voice,
            speed=mx.tts_speed, emotion=mx.tts_emotion,
            on_tick=lambda s, e: _log(f"  TTS {s} ({e}s)"))
        raw_audio = synth
        _log(f"TTS 完成 {audio_duration(raw_audio):.1f}s，扣费 ${tts_cost:.4f}")

    padded: dict[float, Path] = {}
    for sil in mx.silences:
        dest = audio_dir / f"padded_{sil:g}s.mp3"
        if sil <= 0:
            padded[sil] = raw_audio
            continue
        if dest.is_file():
            padded[sil] = dest
            continue
        padded[sil] = prepend_silence(raw_audio, dest, sil)
        _log(f"静默头 {sil:g}s → {dest.name}")

    # 素材上传一次，按静默档复用 URL
    _log(f"上传 image：{Path(args.image).name if not str(args.image).startswith('http') else args.image}")
    image_url = wsclient.upload(args.image)
    audio_urls: dict[float, str] = {}
    for sil, path in padded.items():
        _log(f"上传 audio：{path.name}")
        audio_urls[sil] = wsclient.upload(path)

    image_local = args.image if args.image and not str(args.image).startswith("http") else None

    records: list[dict] = []
    manifest_path = outdir / "manifest.json"
    if manifest_path.is_file():
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
            records = list(prev.get("records") or [])
        except ValueError:
            records = []
    known = {r.get("cell_id"): r for r in records if r.get("cell_id")}

    total_cells = len(cells)
    for i, cell in enumerate(cells, 1):
        existing = known.get(cell.id)
        rec_file = outdir / cell.id / "record.json"
        if rec_file.is_file():
            try:
                existing = json.loads(rec_file.read_text(encoding="utf-8"))
            except ValueError:
                pass
        if existing and existing.get("status") == "ok" and not args.force:
            _log(f"[{i}/{total_cells} {cell.id}] 已完成，跳过")
            known[cell.id] = existing
            continue

        recipe = by_key[cell.recipe]
        seconds = audio_duration(padded[cell.silence])
        ctx = {
            "outdir": outdir, "prompt": read_prompt(cell.prompt_key), "seed": args.seed,
            "timeout": args.timeout, "interval": args.interval, "profile": mx.profile,
            "upscaler": upscaler, "seconds": seconds, "analyze": not args.no_metrics,
            "silence": cell.silence, "still_head": mx.still_head,
            "image_local": image_local,
            "cell_id": cell.id, "log_key": f"{i}/{total_cells} {cell.id}",
        }
        urls = {"image": image_url, "audio": audio_urls[cell.silence]}
        _log(f"[{i}/{total_cells} {cell.id}] 开始  {cell.recipe}  静默{cell.silence:g}s  {cell.prompt_key}")
        rec = _run_one(recipe, urls, ctx)
        rec["cell_id"] = cell.id
        rec["silence"] = cell.silence
        rec["prompt_key"] = cell.prompt_key
        rec["prompt_file"] = cell.prompt_file
        known[cell.id] = rec

        records = all_records(known)
        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "matrix": mx.key,
            "inputs": {"image": args.image, "voice": mx.voice, "script": mx.script_file},
            "cells": [cell.__dict__ for cell in all_cells],
            "records": records,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        index = write_sweep_index(outdir, mx, all_cells, all_records(known),
                                  image=args.image, voice=mx.voice)
        _log(f"对照页已更新 {index}")

    records = all_records(known)
    index = write_sweep_index(outdir, mx, all_cells, records, image=args.image, voice=mx.voice)
    ok_n = sum(1 for r in records if r.get("status") == "ok")
    fail_n = sum(1 for r in records if r.get("status") == "failed")
    spent = sum(r.get("cost_actual", 0) or 0 for r in records)
    print()
    print(summarize(records))
    print(f"\n完成 {ok_n}/{len(cells)}，失败 {fail_n}，本矩阵实测约 ${spent:.3f}")
    print(f"对照页：{index}")
    print("用浏览器打开上面的 html，逐条看开头 3 秒。满意的格子名贴回来即可。")
    try:
        print(f"剩余余额：${wsclient.balance():.4f}")
    except wsclient.WaveSpeedError:
        pass
    return 0 if fail_n == 0 else 1


def cmd_report(args: argparse.Namespace) -> int:
    manifest = Path(args.outdir) / "manifest.json"
    if not manifest.is_file():
        print(f"找不到 {manifest}")
        return 1
    data = json.loads(manifest.read_text(encoding="utf-8"))
    records = data["records"]
    print(summarize(records))
    print(f"\n输入：{data['inputs']}")
    for r in records:
        if r.get("status") == "ok" and not r.get("compliance_passed"):
            print(f"\n{r.get('cell_id') or r.get('recipe')} 入库未过项：")
            for c in r.get("compliance", []):
                if not c["ok"]:
                    print(f"  - {c['name']}: {c['detail']}")
    lines = defect_lines(records)
    if lines.strip():
        print("\n客观缺陷：" + lines)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """对已有视频单独跑客观指标，不调任何付费接口。"""
    import videometrics

    failed = 0
    for path in args.videos:
        result = videometrics.evaluate(
            path, silence_seconds=args.silence, reference_image=args.image)
        print("=" * 70)
        print(path)
        print(result.render())
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        if not result.available:
            failed += 1
    return 1 if failed else 0


def cmd_rank(args: argparse.Namespace) -> int:
    """读盲测导出的 votes.json，算 Bradley-Terry 排名，并和客观指标对一下相关性。"""
    from lab.rating import rank

    votes_path = Path(args.votes) if args.votes else Path(args.outdir) / "votes.json"
    if not votes_path.is_file():
        print(f"找不到 {votes_path}")
        print("在对照页的「盲测」页签投完票，点「导出 votes.json」，放到结果目录里。")
        return 1
    data = json.loads(votes_path.read_text(encoding="utf-8"))
    raw_votes = data.get("votes") or []
    clips = data.get("clips") or {}

    pairs = [(v["winner"], v["a"] if v["winner"] == v["b"] else v["b"])
             for v in raw_votes if v.get("winner")]
    ties = sum(1 for v in raw_votes if not v.get("winner"))
    if not pairs:
        print(f"votes.json 里没有有效比较（{ties} 次判为差不多）")
        return 1

    rows = rank(pairs)
    table = [[str(r["rank"]), r["name"],
              f"{r['score']:+.3f}", f"{r['win']}-{r['loss']}",
              str(clips.get(r["name"], {}).get("recipe", "")),
              str(clips.get(r["name"], {}).get("silence", "")),
              str(clips.get(r["name"], {}).get("prompt", ""))]
             for r in rows]
    print(render_table(["#", "格子", "BT分", "胜-负", "模型", "静默", "提示词"], table))
    print(f"\n共 {len(pairs)} 次有效比较，{ties} 次差不多。"
          f"每条至少比过 3 次分数才比较稳。")

    thin = [r["name"] for r in rows if r["games"] < 3]
    if thin:
        print(f"场次不足 3 的（分数仅供参考）：{', '.join(thin)}")

    correlation = _metric_correlation(Path(args.outdir), rows)
    if correlation:
        print("\n客观指标 vs 你的偏好（Spearman 秩相关）：")
        print(render_table(["指标", "相关系数", "样本"],
                           [[k, f"{v[0]:+.3f}", str(v[1])] for k, v in correlation.items()]))
        print("\n相关系数接近 +1 表示该指标和你的判断同向，可以进权重；"
              "接近 0 说明它测的东西你并不在意。")
    return 0


def _metric_correlation(outdir: Path, ranked: list[dict]) -> dict:
    """把 BT 分数和每个客观指标做秩相关，看哪些指标真的预测你的偏好。"""
    manifest = outdir / "manifest.json"
    if not manifest.is_file():
        return {}
    records = json.loads(manifest.read_text(encoding="utf-8")).get("records") or []
    by_id = {r.get("cell_id"): r for r in records}

    scores: dict[str, float] = {r["name"]: r["score"] for r in ranked}
    series: dict[str, list[tuple[float, float]]] = {}
    for name, score in scores.items():
        metrics = (by_id.get(name) or {}).get("metrics") or {}
        if not metrics.get("available"):
            continue
        flat = {"缺陷_major": -metrics.get("major_count", 0),
                "缺陷_minor": -metrics.get("minor_count", 0)}
        for block in (metrics.get("metrics") or {}).values():
            for key, value in block.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                flat[key] = value
        for key, value in flat.items():
            series.setdefault(key, []).append((score, float(value)))

    out = {}
    for key, points in series.items():
        if len(points) < 4:
            continue
        rho = _spearman([p[0] for p in points], [p[1] for p in points])
        if rho is not None:
            out[key] = (rho, len(points))
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1][0]))[:15])


def _spearman(a: list[float], b: list[float]) -> float | None:
    """秩相关。不引入 scipy：先转秩再算皮尔逊。"""
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    n = len(ra)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb))
    den_a = sum((x - mean_a) ** 2 for x in ra) ** 0.5
    den_b = sum((y - mean_b) ** 2 for y in rb) ** 0.5
    if den_a < 1e-12 or den_b < 1e-12:
        return None
    return round(num / (den_a * den_b), 4)


# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="数字人方案实验台：横向对比多个 API 方案，产出能过平台入库的素材",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出所有方案与单价")
    p.add_argument("--seconds", type=float, default=60)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("plan", help="估算成本，不花钱")
    p.add_argument("--recipes", nargs="+", required=True)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--upscale", default="up-bytedance")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("balance", help="查 WaveSpeed 余额")
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("models", help="查询 WaveSpeed 模型清单")
    p.add_argument("--filter", default="")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("fetch", help="取回一个已提交的任务")
    p.add_argument("prediction_id")
    p.add_argument("-o", "--output")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=10)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("run", help="跑实验")
    p.add_argument("--recipes", nargs="+", required=True,
                   help="方案名 / 组合(screen,tencent) / 路线(A,B,C) / all")
    p.add_argument("--image", help="角色图（本地路径或 URL）")
    p.add_argument("--audio", help="驱动音频（本地路径或 URL）")
    p.add_argument("--script", help="台词文本，没有 --audio 时自动 TTS 合成")
    p.add_argument("--script-file", help="台词稿文件，# 开头的行是注释")
    p.add_argument("--voice", default="qwen:Cherry",
                   help="TTS 音色，见 python3 tts.py --list-voices")
    p.add_argument("--tts-speed", type=float, default=1.0)
    p.add_argument("--tts-emotion", default="neutral")
    p.add_argument("--video", help="模板视频，路线 C 需要")
    p.add_argument("--prompt", default="")
    p.add_argument("--prompt-file")
    p.add_argument("--seed", type=int, default=-1)
    p.add_argument("--seconds", type=float,
                   help="用于成本估算的时长，默认按音频实际时长")
    p.add_argument("--silence", type=float, default=2.0,
                   help="给音频加几秒静默头，满足腾讯「开头闭口 1-3 秒」；0 表示不加")
    p.add_argument("--still-head", action=argparse.BooleanOptionalAction, default=False,
                   help="生成后把静默头画面换成角色图静止帧。默认关闭：会不眨眼、很呆。"
                        "显式 --still-head 才开启")
    p.add_argument("--profile", default="tencent-general",
                   help="入库档位，见 compliance.py")
    p.add_argument("--upscale", default="up-bytedance",
                   help="超分方案。传 none 或 --no-upscale 则不超分")
    p.add_argument("--no-upscale", dest="upscale", action="store_const", const="",
                   help="不做超分（10 秒筛选一般不需要）")
    p.add_argument("--outdir")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--max-cost", type=float, default=1.0,
                   help="成本上限护栏，超过就要 --yes 才跑")
    p.add_argument("--no-analyze", action="store_true", help="跳过抖动分析")
    p.add_argument("--yes", action="store_true", help="不询问直接跑")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("preflight", help="花钱前先检查输入是否可能达标")
    p.add_argument("--image")
    p.add_argument("--audio")
    p.add_argument("--video")
    p.add_argument("--profile", default="tencent-general")
    p.add_argument("--silence", type=float, default=2.0)
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("tts", help="单独合成驱动音频")
    p.add_argument("--text")
    p.add_argument("--script-file", default=str(DEFAULT_SCRIPT_10S),
                   help="台词稿。默认 10 秒筛选稿")
    p.add_argument("-o", "--output", default="speech_10s.mp3")
    p.add_argument("--voice", default="qwen:Cherry")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--emotion", default="neutral")
    p.add_argument("--silence", type=float, default=2.0,
                   help="开头静音秒数，0 表示不加")
    p.set_defaults(func=cmd_tts)

    p = sub.add_parser("report", help="重新汇总已有结果目录")
    p.add_argument("outdir")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("eval", help="对已有视频跑客观指标（不花钱）")
    p.add_argument("videos", nargs="+")
    p.add_argument("--silence", type=float, default=0.0,
                   help="这条素材加了几秒静默头。实际会以音频里量出的为准")
    p.add_argument("--image", help="角色图，给出后会判断成片像不像这张图")
    p.add_argument("--json", action="store_true", help="连原始指标一起打印")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("rank", help="用盲测投票算 Bradley-Terry 排名并检验指标")
    p.add_argument("outdir")
    p.add_argument("--votes", help="votes.json 路径，默认取 <outdir>/votes.json")
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("sweep", help="按参数矩阵一次跑完所有组合（10 秒对比试验）")
    p.add_argument("--image", help="角色图（本地路径）。对照试验的固定输入")
    p.add_argument("--matrix", default="screen10",
                   help="矩阵名，默认 screen10。--list 查看")
    p.add_argument("--list", dest="list_matrices", action="store_true",
                   help="列出内置矩阵，不跑")
    p.add_argument("--outdir")
    p.add_argument("--seed", type=int, default=-1)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--dry-run", action="store_true", help="只打印格子和成本，不调用 API")
    p.add_argument("--force", action="store_true", help="已完成的格子也重跑")
    p.add_argument("--no-metrics", action="store_true",
                   help="跳过客观指标计算（默认会算，见 videometrics/）")
    p.add_argument("--only",
                   help="只跑匹配的格子（子串匹配格子名或方案名）。"
                        "预算紧时用来一个模型一个模型地跑，音频和编号保持一致")
    p.add_argument("--yes", action="store_true", help="余额不够也开跑（会在中途失败）")
    p.set_defaults(func=cmd_sweep)

    args = parser.parse_args()
    try:
        return args.func(args)
    except wsclient.WaveSpeedError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"错误：{exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # TTSError / ComplianceError 都走这里，避免再引入一串 import
        name = type(exc).__name__
        if name in ("TTSError", "ComplianceError", "ValueError"):
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        raise
    except KeyboardInterrupt:
        print("\n已中断（已提交的任务仍在服务端跑，可用 fetch 取回）")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
