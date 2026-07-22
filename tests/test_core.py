"""核心逻辑离线测试：无需网络与真实 API。

覆盖：
- 分段规划 plan_segments 的重叠 / 步进正确性
- workflow 模板固定参数写死 + 用户参数注入
- 端到端本地视频链路：合成视频 -> 切分 -> crossfade 拼接 -> 合并音频
"""

import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roleswap import video_utils as vu
from roleswap import workflow_template as wf


def test_plan_segments_overlap():
    segs = vu.plan_segments(total_frames=300, chunk_frames=96, overlap=12)
    # 首段 [0,96)
    assert segs[0].start == 0 and segs[0].end == 96
    # 步进 = 96 - 12 = 84
    assert segs[1].start == 84 and segs[1].end == 180
    # 覆盖到结尾
    assert segs[-1].end == 300
    # 相邻段重叠恰为 overlap
    for a, b in zip(segs, segs[1:]):
        assert a.end - b.start == 12
    print("plan_segments OK:", [(s.start, s.end) for s in segs])


def test_plan_segments_last_short():
    segs = vu.plan_segments(total_frames=100, chunk_frames=96, overlap=12)
    assert segs[0].end == 96
    assert segs[-1].end == 100  # 末段被裁剪
    print("plan_segments last-short OK")


def test_plan_segments_for_mode():
    single, ov = vu.plan_segments_for_mode(
        total_frames=288, fps=24, slice_mode="single"
    )
    assert len(single) == 1
    assert single[0].start == 0 and single[0].end == 288
    assert ov == 0

    halves, ov2 = vu.plan_segments_for_mode(
        total_frames=288, fps=24, slice_mode="halves", overlap_frames=12
    )
    assert len(halves) == 2
    assert halves[0].start == 0
    assert halves[1].end == 288
    assert ov2 == 12
    print("plan_segments_for_mode OK")


def test_build_payload_relaxed_cap():
    opts = wf.WorkflowOptions(relax_frame_cap=True, frame_load_cap=288)
    payload = wf.build_payload(
        workflow_id="wf-1", video="v", image="i", seed=1,
        options=opts, num_frames=288,
    )
    assert payload["input_values"]["125:value"] == 288
    print("build_payload relaxed cap OK")


def test_build_payload_fixed_params():
    payload = wf.build_payload(
        workflow_id="wf-1",
        video="http://x/v.mp4",
        image="http://x/f.jpg",
        seed=42,
    )
    values = payload["input_values"]
    assert payload["workflow_id"] == "wf-1"
    # 固定参数写死
    assert values["51:blocks_to_swap"] == 40
    assert values["61:tile_x"] == 272
    assert values["50:precision"] == "bf16"
    assert values["125:value"] == wf.FRAME_LOAD_CAP
    # 用户参数注入
    assert values["42:seed"] == 42
    assert values["42:steps"] == 6
    assert values["46:video"] == "http://x/v.mp4"
    assert values["47:image"] == "http://x/f.jpg"
    assert values["42:scheduler"] == "dpm++_sde"
    print("build_payload OK")


def test_build_payload_mode_motion_transfer():
    opts = wf.WorkflowOptions(mode="motion_transfer", positive_prompt="test")
    payload = wf.build_payload(
        workflow_id="wf-1",
        video="v",
        image="i",
        seed=1,
        options=opts,
    )
    values = payload["input_values"]
    assert values["151:value"] is True
    assert values["56:positive_prompt"] == "test"
    print("build_payload mode OK")


def test_frame_cap_guard():
    try:
        wf.build_payload(
            workflow_id="w", video="v", image="i", seed=1,
            num_frames=200,
        )
    except ValueError:
        print("frame_load_cap guard OK")
        return
    raise AssertionError("超过硬上限应抛 ValueError")


def _make_video(path, n_frames, color, fps=24, size=(160, 120), with_audio=False):
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    w, h = size
    writer = cv2.VideoWriter(path, fourcc, fps, (w, h))
    for i in range(n_frames):
        frame = np.full((h, w, 3), color, dtype=np.uint8)
        cv2.putText(frame, str(i), (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1,
                    (255, 255, 255), 2)
        writer.write(frame)
    writer.release()
    if with_audio:
        tmp = path + ".aud.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-f", "lavfi", "-i",
             f"sine=frequency=440:duration={n_frames / fps}",
             "-c:v", "copy", "-c:a", "aac", "-shortest", tmp],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.replace(tmp, path)


def test_end_to_end_local_pipeline():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src.mp4")
        _make_video(src, n_frames=200, color=(50, 100, 150), with_audio=True)

        info = vu.probe_video(src)
        assert info.frame_count >= 190  # 允许编码器少量偏差
        assert vu.has_audio_stream(src) is True

        # 切分
        segs = vu.plan_segments(total_frames=180, chunk_frames=96, overlap=12)
        seg_outputs = []
        for s in segs:
            out = os.path.join(d, f"seg_{s.index}.mp4")
            vu.extract_segment(src, s, out, fps=24)
            seg_outputs.append(out)
            fc = vu.probe_video(out).frame_count
            assert fc > 0

        # crossfade 拼接
        merged = os.path.join(d, "merged.mp4")
        vu.crossfade_concat(seg_outputs, overlap=12, output_path=merged, fps=24)
        assert vu.probe_video(merged).frame_count > 0

        # 合并音频
        audio = vu.extract_audio(src, os.path.join(d, "a.aac"))
        assert audio is not None
        final = os.path.join(d, "final.mp4")
        vu.mux_audio(merged, audio, final)
        assert os.path.exists(final)
        assert vu.has_audio_stream(final) is True

        # 拼接后总帧数应约等于 180（重叠区被融合而非重复计入）
        final_fc = vu.probe_video(final).frame_count
        assert 170 <= final_fc <= 185, f"帧数异常：{final_fc}"
        print(f"end-to-end pipeline OK, final frames={final_fc}")


if __name__ == "__main__":
    test_plan_segments_overlap()
    test_plan_segments_last_short()
    test_plan_segments_for_mode()
    test_build_payload_relaxed_cap()
    test_build_payload_fixed_params()
    test_build_payload_mode_motion_transfer()
    test_frame_cap_guard()
    test_end_to_end_local_pipeline()
    print("\nALL TESTS PASSED")
