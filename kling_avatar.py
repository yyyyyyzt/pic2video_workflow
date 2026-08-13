#!/usr/bin/env python3
"""可灵 AI 数字人（数字角色 2.0）视频生成 —— 命令行工具。

一张角色图 + 一段音频（或一段台词走 TTS）+ 一句提示词 → 数字人口播视频。

用法示例：
    python3 kling_avatar.py --image face.jpg --audio speech.mp3 \
        --prompt-file prompts/electricity_safety_recommended.txt --output result.mp4

    # 不传音频，用台词自动配音
    python3 kling_avatar.py --image face.jpg --script-file prompts/xxx_script.txt \
        --voice genshin_vindi2 --output result.mp4

想用网页界面（上传、TTS、任务进度）：python3 server.py 然后打开 http://127.0.0.1:8000
API 核心逻辑在 klingclient.py，Web 服务与本 CLI 共用。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import klingclient as kc


def load_text_file(path: str, what: str) -> str:
    """读取提示词/台词文件。以 # 开头的整行视为注释；空行保留为段落分隔。"""
    p = Path(path)
    if not p.is_file():
        sys.exit(f"错误：找不到{what}文件 {path}")
    lines = [line.rstrip() for line in p.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("#")]
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if not text:
        sys.exit(f"错误：{what}文件为空（或全是注释）: {path}")
    return text


def poll_until_done(token_factory, task_id: str, interval: int, timeout: int) -> str:
    """轮询直到 succeed，返回视频 URL。"""
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        task = kc.get_avatar_task(token_factory(), task_id)
        print(f"[{int(time.time() - started):>4d}s] 状态: {task['status']}")

        if task["status"] == "succeed":
            if not task["videos"]:
                sys.exit("任务成功但未返回视频地址")
            video = task["videos"][0]
            print(f"生成成功！时长 {video.get('duration', '?')} 秒")
            return video["url"]
        if task["status"] == "failed":
            sys.exit(f"任务失败: {task['message'] or '(无失败原因)'}")

        time.sleep(interval)
    sys.exit(f"超时：{timeout} 秒内未完成，可稍后用 task_id={task_id} 手动查询")


def run_stabilize(output: str, method: str) -> None:
    """把成片改名为 *_raw.mp4，后处理结果写回原路径（原始文件始终留底）。"""
    out = Path(output)
    raw = out.with_name(f"{out.stem}_raw{out.suffix}")
    out.replace(raw)
    print(f"\n原始成片已保留为 {raw}")
    print(f"运行后处理（method={method}）…")

    cmd = [sys.executable, str(Path(__file__).with_name("stabilize.py")),
           str(raw), "-o", str(out), "--method", method]
    if subprocess.run(cmd).returncode != 0:
        raw.replace(out)  # 后处理失败就还原，不留下半成品
        sys.exit("后处理失败，已还原原始成片。可单独调试：python3 stabilize.py <视频> --method analyze")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="可灵数字人视频生成：角色图 + 音频/台词 + 提示词 → 视频",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--image",
                        help="角色图：本地文件(jpg/jpeg/png, ≤10MB, 宽高≥300px, 宽高比 1:2.5~2.5:1) 或 URL")

    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument("--audio", help="驱动音频：本地文件(mp3/wav/m4a/aac, ≤5MB) 或 URL")
    audio_group.add_argument("--audio-id", help="已有的可灵音频 ID")
    audio_group.add_argument("--script", help="台词文本，自动走 TTS 生成配音")
    audio_group.add_argument("--script-file", help="从文件读取台词，自动走 TTS（# 行为注释）")

    parser.add_argument("--voice", default="genshin_vindi2", help="TTS 音色 ID（--list-voices 查看）")
    parser.add_argument("--voice-language", default="zh", choices=["zh", "en"], help="TTS 语种")
    parser.add_argument("--voice-speed", type=float, default=1.0, help="TTS 语速 0.8~2.0")
    parser.add_argument("--list-voices", action="store_true", help="列出可用音色后退出")

    parser.add_argument("--prompt", default="", help="提示词：描述动作、情绪、镜头，≤2500 字符")
    parser.add_argument("--prompt-file", help="从文件读取提示词（# 行为注释），方便编辑测试")
    parser.add_argument("--mode", choices=["std", "pro"], default="std", help="std=标准 / pro=专家(质量更高)")
    parser.add_argument("-o", "--output", "-output", default="avatar_output.mp4",
                        help="输出视频路径")
    parser.add_argument("--poll-interval", type=int, default=15, help="轮询间隔（秒）")
    parser.add_argument("--timeout", type=int, default=3600, help="等待超时（秒）")
    parser.add_argument("--stabilize", nargs="?", const="track", default=None,
                        help="生成后跑抖动后处理（需 opencv-python + ffmpeg）。可传 "
                             "track/vidstab/deflicker/interp 或逗号串联；不传值等于 track")
    args = parser.parse_args()

    if args.list_voices:
        print("可用音色（官方对照表见 klingclient.py 注释里的链接）：")
        for v in kc.VOICES:
            print(f"  {v['language']}  {v['id']:<26} {v['label']}")
        return

    if not args.image:
        sys.exit("错误：--image 必填（查看音色用 --list-voices）")
    if not (args.audio or args.audio_id or args.script or args.script_file):
        sys.exit("错误：请给出音频来源之一：--audio / --audio-id / --script / --script-file")

    if args.prompt and args.prompt_file:
        sys.exit("错误：--prompt 与 --prompt-file 只能二选一")
    prompt = load_text_file(args.prompt_file, "提示词") if args.prompt_file else args.prompt
    if len(prompt) > kc.PROMPT_MAX_CHARS:
        sys.exit(f"错误：提示词 {len(prompt)} 字符，超过 {kc.PROMPT_MAX_CHARS} 上限")

    kc.load_dotenv()
    try:
        auth_mode, token_factory = kc.resolve_auth()
    except kc.KlingError as exc:
        sys.exit(f"错误：{exc}")
    print(f"鉴权模式: {auth_mode} | 域名: {kc.base_url()}")
    if prompt:
        print(f"提示词: {len(prompt)} 字符")

    audio_id = args.audio_id
    try:
        if args.script or args.script_file:
            script = load_text_file(args.script_file, "台词") if args.script_file else args.script
            print(f"台词 {len(script)} 字 → TTS 合成（音色 {args.voice}, 语速 {args.voice_speed}）…")
            tts = kc.synthesize_speech(token_factory(), script, args.voice,
                                       args.voice_language, args.voice_speed)
            audio_id = tts["audio_id"]
            print(f"配音完成：{tts['duration']:.1f} 秒，audio_id={audio_id}")
            if tts["duration"] > kc.AUDIO_MAX_SECONDS:
                print(f"注意：音频 {tts['duration']:.1f} 秒超过文档标注的 {kc.AUDIO_MAX_SECONDS} 秒上限，"
                      "接口可能拒绝；如失败请拆分台词")

        task_id = kc.create_avatar_task(
            token_factory(), image=args.image, audio=args.audio,
            audio_id=audio_id, prompt=prompt, mode=args.mode,
        )
    except kc.KlingError as exc:
        sys.exit(f"错误：{exc}")

    print(f"任务已提交，task_id = {task_id}")
    video_url = poll_until_done(token_factory, task_id, args.poll_interval, args.timeout)

    print(f"下载视频: {video_url}")
    out = kc.download(video_url, args.output)
    print(f"已保存到 {out}（{out.stat().st_size / 1024 / 1024:.1f}MB）")

    if args.stabilize:
        run_stabilize(args.output, args.stabilize)


if __name__ == "__main__":
    main()
