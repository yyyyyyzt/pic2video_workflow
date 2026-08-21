#!/usr/bin/env python3
"""结果汇总与 HTML 对照页。

对照页有两个页签，对应两种不同的看法：

  网格   每条视频 + 开头抽帧 + 客观指标 + 缺陷清单。用来定位「哪里坏了」。
  盲测   一次只给两条，你选哪个好，不显示模型名。用来产出校准标注。

盲测那一页是整套评价体系能不能落地的关键：客观指标必须先证明和你的偏好一致，
才有资格替你排序。看得见模型名就会被先验影响（知道是 OmniHuman 就倾向给高分），
所以盲测页把参数全隐藏，只在导出的结果里还原。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .table import render_table


def summarize(records: list[dict]) -> str:
    rows = []
    for r in records:
        name = r.get("cell_id") or r.get("recipe") or "?"
        if r.get("status") != "ok":
            rows.append([name, r.get("status", "?"), "-", "-", "-", "-",
                         (r.get("error", "") or "")[:40]])
            continue
        probe = r.get("final_probe", {})
        metrics = r.get("metrics") or {}
        if metrics.get("available"):
            quality = f"{metrics.get('major_count', 0)}重/{metrics.get('minor_count', 0)}轻"
        else:
            quality = "-"
        extra = []
        if r.get("silence") is not None:
            extra.append(f"sil={r['silence']:g}")
        if r.get("prompt_key"):
            extra.append(str(r["prompt_key"]))
        extra.append(f"{r.get('elapsed_seconds', 0):.0f}s")
        rows.append([
            name,
            "通过" if r.get("compliance_passed") else "不通过",
            f"{probe.get('width')}×{probe.get('height')}",
            f"{probe.get('duration', 0):.0f}s",
            quality,
            f"${r.get('cost_actual', 0):.3f}",
            " ".join(extra),
        ])
    total = sum(r.get("cost_actual", 0) or 0 for r in records)
    table = render_table(
        ["方案", "入库", "分辨率", "时长", "客观缺陷", "成本(实测)", "参数/耗时"], rows)
    return f"{table}\n\n实际总花费 ${total:.3f}"


def defect_lines(records: list[dict]) -> str:
    """把所有缺陷按维度铺开，便于一眼看出共性问题。"""
    out = []
    for r in records:
        metrics = r.get("metrics") or {}
        defects = metrics.get("defects") or []
        if not defects:
            continue
        out.append(f"\n{r.get('cell_id') or r.get('recipe')}：")
        for d in defects:
            at = f" @{d['at_seconds']:g}s" if d.get("at_seconds") is not None else ""
            out.append(f"  ({d['severity']}) {d['dimension']}/{d['code']}{at}"
                       f" {d['detail']}")
    return "\n".join(out)


def _rel(path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


def _metric_chips(metrics: dict) -> str:
    """挑几个最能说明问题的指标做成小标签，完整数据在 record.json 里。"""
    if not metrics.get("available"):
        return f'<span class="chip warn">指标不可用</span>'
    blocks = metrics.get("metrics") or {}
    lip = blocks.get("协调性", {})
    alive = blocks.get("活性", {})
    ident = blocks.get("一致性", {})
    picks = [
        ("口型相关", lip.get("corr_best")),
        ("偏移ms", lip.get("lag_ms") if lip.get("lag_reliable") else None),
        ("静默开口比", alive.get("silence_open_vs_speech_ratio")),
        ("眨眼", alive.get("blink_count")),
        ("身份漂移", ident.get("embed_drift_observed")),
    ]
    chips = [f'<span class="chip">{html.escape(k)} {v}</span>'
             for k, v in picks if v is not None]
    major = metrics.get("major_count", 0)
    minor = metrics.get("minor_count", 0)
    cls = "bad" if major else ("warn" if minor else "good")
    chips.insert(0, f'<span class="chip {cls}">{major}重 / {minor}轻</span>')
    return "".join(chips)


def _defect_html(metrics: dict) -> str:
    defects = (metrics or {}).get("defects") or []
    if not defects:
        return '<p class="meta">未检出缺陷</p>'
    items = []
    for d in defects:
        at = f" @{d['at_seconds']:g}s" if d.get("at_seconds") is not None else ""
        cls = "bad" if d["severity"] == "major" else "warn"
        items.append(
            f'<li class="{cls}">{html.escape(d["dimension"])}/'
            f'{html.escape(d["code"])}{at} — {html.escape(d["detail"])}</li>')
    return f'<ul class="defects">{"".join(items)}</ul>'


def write_sweep_index(outdir: Path, matrix, cells, records: list[dict], *,
                      image: str, voice: str) -> Path:
    by_id = {r.get("cell_id"): r for r in records}
    cards, blind = [], []

    for cell in cells:
        rec = by_id.get(cell.id) or {}
        status = rec.get("status", "pending")
        video = rec.get("final") or rec.get("raw") or ""
        playable = bool(video) and Path(video).is_file()
        video_rel = _rel(video, outdir) if playable else ""
        thumbs = "".join(
            f'<img src="{_rel(t, outdir)}" alt="">'
            for t in (rec.get("thumbs") or []))
        metrics = rec.get("metrics") or {}
        cost = rec.get("cost_actual")
        cost_s = f"${cost:.3f}" if isinstance(cost, (int, float)) else "-"
        probe = rec.get("final_probe") or rec.get("raw_probe") or {}
        spec = (f"{probe.get('width', '?')}×{probe.get('height', '?')} "
                f"{probe.get('duration', 0):.1f}s") if probe else ""
        media = (f'<video controls preload="metadata" src="{video_rel}"></video>'
                 if playable else
                 f'<div class="miss">{html.escape(rec.get("error") or status)}</div>')

        cards.append(f"""
<article class="card {status}" data-recipe="{html.escape(cell.recipe)}"
         data-silence="{cell.silence:g}" data-prompt="{html.escape(cell.prompt_key)}">
  <header>
    <strong>{html.escape(cell.id)}</strong>
    <span class="pill">{html.escape(cell.recipe)}</span>
    <span class="pill">静默 {cell.silence:g}s</span>
    <span class="pill">{html.escape(cell.prompt_key)}</span>
  </header>
  {media}
  <div class="thumbs">{thumbs}</div>
  <p class="meta">{html.escape(spec)}　{cost_s}</p>
  <div class="chips">{_metric_chips(metrics)}</div>
  {_defect_html(metrics)}
</article>""")

        if playable:
            blind.append({"id": cell.id, "src": video_rel,
                          "recipe": cell.recipe, "silence": cell.silence,
                          "prompt": cell.prompt_key})

    payload = json.dumps(blind, ensure_ascii=False)
    html_doc = _TEMPLATE.format(
        title=html.escape(f"sweep {matrix.key}"),
        label=html.escape(matrix.label),
        voice=html.escape(voice),
        image=html.escape(Path(image).name if image else "?"),
        cards="\n".join(cards),
        blind_json=payload,
        total=len(blind),
    )
    path = outdir / "index.html"
    path.write_text(html_doc, encoding="utf-8")
    return path


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 14px/1.5 system-ui, -apple-system, sans-serif; background:#111; color:#eee; margin:0; }}
  header.page {{ padding:20px 24px 4px; }}
  h1 {{ margin:0 0 6px; font-size:20px; }}
  .note {{ color:#bbb; max-width:78ch; }}
  nav {{ display:flex; gap:8px; padding:12px 24px; border-bottom:1px solid #333; }}
  nav button {{ background:#222; color:#ddd; border:1px solid #444; border-radius:8px;
               padding:6px 14px; cursor:pointer; font-size:14px; }}
  nav button.on {{ background:#2d5; color:#062; border-color:#2d5; font-weight:600; }}
  section.tab {{ display:none; }} section.tab.on {{ display:block; }}
  .filters {{ padding:14px 24px 0; display:flex; gap:12px; flex-wrap:wrap; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
          gap:16px; padding:16px 24px 48px; }}
  .card {{ background:#1c1c1c; border:1px solid #333; border-radius:10px; padding:12px; }}
  .card.failed {{ border-color:#833; }} .card.pending {{ opacity:.5; }}
  .card header {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; }}
  .pill {{ font-size:12px; background:#2a2a2a; padding:2px 8px; border-radius:999px; }}
  video {{ width:100%; border-radius:8px; background:#000; }}
  .thumbs {{ display:flex; gap:4px; margin-top:8px; }}
  .thumbs img {{ width:25%; border-radius:4px; }}
  .meta {{ color:#aaa; font-size:12px; margin:8px 0 0; }}
  .chips {{ margin:8px 0 0; display:flex; flex-wrap:wrap; gap:4px; }}
  .chip {{ font-size:11px; background:#243; padding:2px 7px; border-radius:6px; color:#cfc; }}
  .chip.good {{ background:#164; color:#bfd; }}
  .chip.warn {{ background:#553d10; color:#fd9; }}
  .chip.bad {{ background:#611; color:#fbb; }}
  ul.defects {{ margin:8px 0 0; padding-left:18px; font-size:12px; color:#ccc; }}
  ul.defects li.bad {{ color:#f99; }} ul.defects li.warn {{ color:#fc9; }}
  .miss {{ padding:40px 12px; text-align:center; color:#888; }}
  /* 盲测 */
  .blind {{ padding:16px 24px 48px; max-width:1100px; }}
  .pair {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .pair figure {{ margin:0; }}
  .pair figcaption {{ text-align:center; color:#999; margin-top:6px; }}
  .choose {{ display:flex; gap:10px; justify-content:center; margin:18px 0; flex-wrap:wrap; }}
  .choose button {{ padding:10px 20px; border-radius:8px; border:1px solid #555;
                   background:#242424; color:#eee; cursor:pointer; font-size:15px; }}
  .choose button:hover {{ border-color:#2d5; }}
  .progress {{ color:#aaa; }}
  .actions {{ margin-top:20px; display:flex; gap:10px; flex-wrap:wrap; }}
  .actions button {{ padding:8px 14px; border-radius:8px; border:1px solid #555;
                    background:#242424; color:#eee; cursor:pointer; }}
  pre {{ background:#0c0c0c; border:1px solid #333; border-radius:8px; padding:12px;
        overflow:auto; max-height:320px; font-size:12px; }}
</style>
</head>
<body>
<header class="page">
  <h1>{label}</h1>
  <p class="note">音色 {voice}　角色图 {image}。抽帧从左到右约 0.3s / 1s / 2s / 3s。
  对照时重点看开头 3 秒：有没有深吸气、会不会眨眼、开口是否从闭嘴直接开始。
  客观指标只是缺陷探测器，指标干净不代表好看——所以请用「盲测」页产出你的偏好，
  那份数据才是校准指标权重的依据。</p>
</header>

<nav>
  <button id="tab-grid" class="on" onclick="showTab('grid')">网格对照</button>
  <button id="tab-blind" onclick="showTab('blind')">盲测（两两选优）</button>
</nav>

<section id="sec-grid" class="tab on">
  <div class="filters">
    <label>模型 <select id="f-recipe"><option value="">全部</option></select></label>
    <label>静默 <select id="f-silence"><option value="">全部</option></select></label>
    <label>提示词 <select id="f-prompt"><option value="">全部</option></select></label>
  </div>
  <div class="grid">
{cards}
  </div>
</section>

<section id="sec-blind" class="tab">
  <div class="blind">
    <p class="progress" id="progress"></p>
    <div class="pair">
      <figure><video id="vidA" controls preload="metadata"></video><figcaption>左</figcaption></figure>
      <figure><video id="vidB" controls preload="metadata"></video><figcaption>右</figcaption></figure>
    </div>
    <div class="choose">
      <button onclick="vote('A')">← 左边更好</button>
      <button onclick="vote('tie')">差不多 / 跳过</button>
      <button onclick="vote('B')">右边更好 →</button>
    </div>
    <div class="actions">
      <button onclick="download()">导出 votes.json</button>
      <button onclick="reset()">清空重来</button>
      <button onclick="toggleRaw()">看已投票记录</button>
    </div>
    <pre id="raw" style="display:none"></pre>
    <p class="note">导出的 votes.json 放回结果目录，然后跑
    <code>python3 avatar_lab.py rank &lt;结果目录&gt;</code>
    就能得到 Bradley-Terry 排名，并和客观指标做相关性检验。</p>
  </div>
</section>

<script>
const CLIPS = {blind_json};
const TOTAL = {total};
const KEY = "sweep-votes-" + location.pathname;
let votes = JSON.parse(localStorage.getItem(KEY) || "[]");
let current = null;

function showTab(name) {{
  for (const t of ["grid", "blind"]) {{
    document.getElementById("sec-" + t).classList.toggle("on", t === name);
    document.getElementById("tab-" + t).classList.toggle("on", t === name);
  }}
  if (name === "blind") nextPair();
}}

for (const key of ["recipe", "silence", "prompt"]) {{
  const sel = document.getElementById("f-" + key);
  const vals = [...new Set([...document.querySelectorAll(".card")].map(c => c.dataset[key]))];
  for (const v of vals) {{
    const o = document.createElement("option"); o.value = v; o.textContent = v;
    sel.appendChild(o);
  }}
  sel.addEventListener("change", applyFilters);
}}
function applyFilters() {{
  const keys = ["recipe", "silence", "prompt"];
  const f = Object.fromEntries(keys.map(k => [k, document.getElementById("f-" + k).value]));
  for (const c of document.querySelectorAll(".card")) {{
    c.style.display = keys.every(k => !f[k] || c.dataset[k] === f[k]) ? "" : "none";
  }}
}}

/* 优先安排「比较次数最少」的两条，让每条视频的场次尽量均衡：
   Bradley-Terry 对场次不均衡很敏感，只比过一次的项分数不可信。 */
function pairKey(a, b) {{ return [a, b].sort().join("|"); }}
function nextPair() {{
  if (CLIPS.length < 2) {{
    document.getElementById("progress").textContent = "可播放的视频不足两条";
    return;
  }}
  const judged = new Set(votes.map(v => pairKey(v.a, v.b)));
  const games = {{}};
  for (const c of CLIPS) games[c.id] = 0;
  for (const v of votes) {{ games[v.a] = (games[v.a] || 0) + 1; games[v.b] = (games[v.b] || 0) + 1; }}

  let best = null, bestCost = Infinity;
  for (let i = 0; i < CLIPS.length; i++) {{
    for (let j = i + 1; j < CLIPS.length; j++) {{
      if (judged.has(pairKey(CLIPS[i].id, CLIPS[j].id))) continue;
      const cost = games[CLIPS[i].id] + games[CLIPS[j].id] + Math.random() * 0.5;
      if (cost < bestCost) {{ bestCost = cost; best = [CLIPS[i], CLIPS[j]]; }}
    }}
  }}
  const totalPairs = CLIPS.length * (CLIPS.length - 1) / 2;
  document.getElementById("progress").textContent =
    `已投 ${{votes.length}} / 共 ${{totalPairs}} 对可比（不必全投，每条比过 3~4 次就够排序）`;
  if (!best) {{ document.getElementById("progress").textContent += " — 全部比完了"; return; }}

  if (Math.random() < 0.5) best = [best[1], best[0]];   // 随机左右，避免位置偏好
  current = {{ a: best[0], b: best[1] }};
  document.getElementById("vidA").src = best[0].src;
  document.getElementById("vidB").src = best[1].src;
}}

function vote(choice) {{
  if (!current) return;
  votes.push({{
    a: current.a.id, b: current.b.id,
    winner: choice === "tie" ? null : (choice === "A" ? current.a.id : current.b.id),
    at: new Date().toISOString(),
  }});
  localStorage.setItem(KEY, JSON.stringify(votes));
  nextPair();
}}
function reset() {{ votes = []; localStorage.removeItem(KEY); nextPair(); }}
function toggleRaw() {{
  const el = document.getElementById("raw");
  el.style.display = el.style.display === "none" ? "block" : "none";
  el.textContent = JSON.stringify(votes, null, 2);
}}
function download() {{
  const meta = {{}};
  for (const c of CLIPS) meta[c.id] = {{recipe: c.recipe, silence: c.silence, prompt: c.prompt}};
  const blob = new Blob([JSON.stringify({{clips: meta, votes: votes}}, null, 2)],
                        {{type: "application/json"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "votes.json"; a.click();
}}
</script>
</body>
</html>
"""
