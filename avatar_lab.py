#!/usr/bin/env python3
"""数字人方案实验台：同一张图 + 同一段音频，横向跑多个方案并给出可比对的结论。

它解决的不是「生成一条视频」，而是「在十几个候选模型里挑出哪个能过平台入库」。
所以每个方案跑完都会自动走完整条流水线：

    上传素材 → 生成 → 超分到 1080p → 规范化(比例/帧率/容器) → 入库合规校验 → 抖动指标

最后汇总成一张表，用客观数字排序，而不是靠肉眼感觉。

常用命令：
    python3 avatar_lab.py balance                     查余额
    python3 avatar_lab.py list                        看所有方案和单价
    python3 avatar_lab.py plan --seconds 60 --recipes A    先算钱，不花钱
    python3 avatar_lab.py run --image a.jpg --audio b.mp3 --recipes skyreels-std
    python3 avatar_lab.py report data/lab/20260819-1200 重新汇总已有结果
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import wsclient
from klingclient import load_dotenv
from recipes import RECIPES, ROUTE_LABELS, Recipe, build_payload, resolve

DEFAULT_OUTDIR = Path("data/lab")


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _width(text: str) -> int:
    """按终端显示宽度算长度，中文占 2 列。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = [max(_width(headers[i]), *(_width(r[i]) for r in rows)) if rows
              else _width(headers[i]) for i in range(cols)]

    def line(cells: list[str]) -> str:
        return "  ".join(c + " " * (widths[i] - _width(c)) for i, c in enumerate(cells))

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# list / plan
# ---------------------------------------------------------------------------

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
        print(_table(["方案", "说明", "输入", "分辨率", f"{seconds:g}s 成本", "备注"], rows))
    print(f"\n成本按 {seconds:g} 秒计。带 ? 的是粗估：WaveSpeed 的 base_price 单位不统一"
          "（有的每秒、有的每 5 秒），官方也声明以实际扣费为准。")
    print("跑过一次之后会用实测单价（data/cost_calibration.json），? 消失即为实测。")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    picked = resolve(args.recipes)
    seconds = args.seconds
    upscale = RECIPES[args.upscale] if args.upscale else None

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

    print(_table(["方案", "说明", "生成", "超分", "小计"], rows))
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
    print(_table(["model_id", "type", "base_price/5s"], rows))
    print(f"\n共 {len(hits)} / {len(models)} 个模型")
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
    if args.prompt_file:
        lines = Path(args.prompt_file).read_text(encoding="utf-8").splitlines()
        return " ".join(l.strip() for l in lines
                        if l.strip() and not l.strip().startswith("#"))
    return args.prompt or ""


def _analyze(video: Path) -> dict:
    """调 stabilize.py 的分析能力拿抖动指标；没装 opencv 就跳过。"""
    try:
        import stabilize  # noqa: F401  仅用于探测依赖是否齐全
    except Exception:
        return {}
    import subprocess
    out = subprocess.run([sys.executable, "stabilize.py", str(video), "--method", "analyze"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    metrics: dict = {"raw": out.stdout}
    for line in out.stdout.splitlines():
        if "抖动加速度" in line:
            nums = [t for t in line.replace("　", " ").split() if _isnum(t)]
            keys = ["accel_median", "accel_p95", "accel_p99", "accel_max"]
            metrics.update(dict(zip(keys, (float(n) for n in nums))))
    return metrics


def _isnum(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


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
    from mediaprep import normalize

    workdir: Path = ctx["outdir"] / recipe.key
    workdir.mkdir(parents=True, exist_ok=True)
    record: dict = {"recipe": recipe.key, "label": recipe.label, "route": recipe.route,
                    "model": recipe.model, "started_at": datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()

    try:
        payload = build_payload(
            recipe,
            image_url=urls.get("image", ""), audio_url=urls.get("audio", ""),
            video_url=urls.get("video", ""), prompt=ctx["prompt"], seed=ctx["seed"],
        )
        _log(f"[{recipe.key}] 提交 {recipe.model}")
        billable = recipe.billable_seconds(ctx["seconds"])
        result, gen_cost = _billed(
            lambda: wsclient.run(recipe.model, payload, timeout=ctx["timeout"],
                                 interval=ctx["interval"],
                                 on_tick=lambda s, e: _log(f"[{recipe.key}]   {s} ({e}s)")),
            recipe.model, billable)
        record["prediction_id"] = result["prediction_id"]
        record["cost_generate"] = gen_cost

        raw = wsclient.download(result["outputs"][0], workdir / "01_raw.mp4")
        record["raw"] = str(raw)
        record["raw_probe"] = probe(raw)
        _log(f"[{recipe.key}] 生成完成 {record['raw_probe']['width']}×"
             f"{record['raw_probe']['height']} {record['raw_probe']['duration']:.1f}s"
             f"，实际扣费 ${gen_cost:.4f}")

        current = raw
        up_cost = 0.0

        # 超分：原生 1080p 的方案跳过，避免白花钱和二次劣化
        upscaler: Recipe | None = ctx["upscaler"]
        if upscaler and not recipe.native_1080p and recipe.route != "U":
            _log(f"[{recipe.key}] 超分 {upscaler.model}")
            up_url = wsclient.upload(current)
            up_payload = dict(upscaler.params, video=up_url)
            up, up_cost = _billed(
                lambda: wsclient.run(upscaler.model, up_payload, timeout=ctx["timeout"],
                                     interval=ctx["interval"]),
                upscaler.model, record["raw_probe"]["duration"])
            current = wsclient.download(up["outputs"][0], workdir / "02_upscaled.mp4")
            record["upscaled"] = str(current)
            record["cost_upscale"] = up_cost

        # 规范化到平台档位
        final = normalize(current, workdir / "03_final.mp4", profile=ctx["profile"])
        record["final"] = str(final)
        record["final_probe"] = probe(final)

        report = check(final, ctx["profile"])
        record["compliance_passed"] = report.passed
        record["compliance"] = [{"name": c.name, "ok": c.ok, "detail": c.detail}
                                for c in report.checks]
        _log(f"[{recipe.key}] 入库校验 {'通过' if report.passed else '不通过'}")

        if ctx["analyze"]:
            record["stability"] = _analyze(final)

        record["status"] = "ok"
        record["cost_actual"] = round(gen_cost + up_cost, 4)

    except (wsclient.WaveSpeedError, ComplianceError, ValueError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        _log(f"[{recipe.key}] 失败：{exc}")

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

    outdir = Path(args.outdir or DEFAULT_OUTDIR / datetime.now().strftime("%Y%m%d-%H%M%S"))
    outdir.mkdir(parents=True, exist_ok=True)

    # 音频先加静默头，再据此算真实时长与成本
    audio_path = args.audio
    if audio_path and args.silence > 0:
        padded = outdir / "input_audio_padded.mp3"
        prepend_silence(audio_path, padded, args.silence)
        _log(f"已给音频加 {args.silence:g}s 静默头 → {padded.name}"
             f"（腾讯要求开头闭口 1~3 秒）")
        audio_path = str(padded)

    seconds = args.seconds or (audio_duration(audio_path) if audio_path else 10.0)
    upscaler = RECIPES[args.upscale] if args.upscale else None

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

    ctx = {
        "outdir": outdir, "prompt": _read_prompt(args), "seed": args.seed,
        "timeout": args.timeout, "interval": args.interval, "profile": args.profile,
        "upscaler": upscaler, "seconds": seconds, "analyze": not args.no_analyze,
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
                   "prompt": ctx["prompt"], "seconds": seconds},
        "profile": args.profile, "upscale": args.upscale,
        "records": records,
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(_summarize(records))
    print(f"\n结果目录：{outdir}")
    try:
        print(f"剩余余额：${wsclient.balance():.4f}")
    except wsclient.WaveSpeedError:
        pass
    return 0


def _summarize(records: list[dict]) -> str:
    rows = []
    for r in records:
        if r.get("status") != "ok":
            rows.append([r["recipe"], r.get("status", "?"), "-", "-", "-", "-",
                         (r.get("error", "") or "")[:40]])
            continue
        p = r.get("final_probe", {})
        st = r.get("stability", {})
        rows.append([
            r["recipe"],
            "通过" if r.get("compliance_passed") else "不通过",
            f"{p.get('width')}×{p.get('height')}",
            f"{p.get('fps', 0):g}fps",
            f"{p.get('duration', 0):.0f}s",
            f"${r.get('cost_actual', 0):.3f}",
            (f"p99={st['accel_p99']:.2f}" if "accel_p99" in st else "-") +
            f" {r.get('elapsed_seconds', 0):.0f}s",
        ])
    total = sum(r.get("cost_actual", 0) for r in records)
    table = _table(["方案", "入库", "分辨率", "帧率", "时长", "成本(实测)", "抖动p99/耗时"], rows)
    return f"{table}\n\n实际总花费 ${total:.3f}"


def cmd_report(args: argparse.Namespace) -> int:
    manifest = Path(args.outdir) / "manifest.json"
    if not manifest.is_file():
        print(f"找不到 {manifest}")
        return 1
    data = json.loads(manifest.read_text(encoding="utf-8"))
    print(_summarize(data["records"]))
    print(f"\n输入：{data['inputs']}")
    for r in data["records"]:
        if r.get("status") == "ok" and not r.get("compliance_passed"):
            print(f"\n{r['recipe']} 未过项：")
            for c in r.get("compliance", []):
                if not c["ok"]:
                    print(f"  - {c['name']}: {c['detail']}")
    return 0


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
                   help="方案名/路线名(A,B,C)/all，逗号或空格分隔")
    p.add_argument("--image", help="角色图（本地路径或 URL）")
    p.add_argument("--audio", help="驱动音频（本地路径或 URL）")
    p.add_argument("--video", help="模板视频，路线 C 需要")
    p.add_argument("--prompt", default="")
    p.add_argument("--prompt-file")
    p.add_argument("--seed", type=int, default=-1)
    p.add_argument("--seconds", type=float,
                   help="用于成本估算的时长，默认按音频实际时长")
    p.add_argument("--silence", type=float, default=2.0,
                   help="给音频加几秒静默头，满足腾讯「开头闭口 1-3 秒」；0 表示不加")
    p.add_argument("--profile", default="tencent-general",
                   help="入库档位，见 compliance.py")
    p.add_argument("--upscale", default="up-bytedance",
                   help="超分方案，传空字符串则不超分")
    p.add_argument("--outdir")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--max-cost", type=float, default=1.0,
                   help="成本上限护栏，超过就要 --yes 才跑")
    p.add_argument("--no-analyze", action="store_true", help="跳过抖动分析")
    p.add_argument("--yes", action="store_true", help="不询问直接跑")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="重新汇总已有结果目录")
    p.add_argument("outdir")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    try:
        return args.func(args)
    except wsclient.WaveSpeedError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"错误：{exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断（已提交的任务仍在服务端跑，可用 fetch 取回）")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
