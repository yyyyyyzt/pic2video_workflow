"""自托管 Wan2.2-Animate 引擎（口播场景主引擎）。

为什么口播选它而不是 SCAIL-2
============================
Wan2.2-Animate 把**身体动作**与**面部表情**解耦成两路条件分别注入：骨架
（``pose_video``）做空间对齐的肢体控制，人脸裁剪（``face_video``，512×512）
经隐式特征走注意力注入表情。对口播视频这种"身体几乎不动、面部微表情和口型
占绝对主导"的素材，这条专门的面部通路让眨眼、眼神、口型的复刻明显更细腻——
这正是 SCAIL-2 端到端方案相对薄弱的地方（它的强项是复杂 3D 动作与非人角色）。

全原生节点的预处理链（无需任何第三方自定义节点）
==============================================
官方 ComfyUI 工作流依赖 ``comfyui_controlnet_aux`` 的 DWPose 和 KJNodes 的
``Points Editor``。后者是**交互式**节点（要手点人物位置），API 自动化根本用不了。
本引擎改用 ComfyUI 原生节点复现同样的输入：

    RTDETR_detect(class_name=person)          → 人体框（多人检测的前置条件）
        → SDPoseKeypointExtractor            → 全身关键点（含 68 点人脸）
            → SDPoseDrawKeypoints            → pose_video（骨架渲染）
            → SDPoseFaceBBoxes + CropByBBoxes → face_video（512×512 人脸序列）
    SAM3_VideoTrack + SAM3_TrackToMask        → character_mask（替代 Points Editor）

替换模式（mix）额外接入 ``background_video``（= 驱动视频原片）与
``character_mask``：模型只在掩码区域重绘新角色，掩码外保留原始背景与光照，
再叠加官方 relight LoRA 让人物与环境光色一致。
动作迁移模式（move）不接这两路，背景来自参考图。

长视频锚定与 SCAIL-2 同构：``continue_motion`` 收上一块的解码输出，
节点取其末尾 ``continue_motion_max_frames``（训练值 5）帧编码后冻结为新块
latent 头部。窗口是 77 帧（约 4.8s @16fps）。

采样后只需按 ``trim_latent`` 裁掉参考图占用的 latent 头；``continue_motion``
帧已落在 ``length`` 窗口内，应保留给 processor 做重叠区 crossfade
（不要再用 ``trim_image`` 裁掉，否则会得到 77-5=72 帧）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

import requests

from ..config import WanAnimateConfig
from ..errors import EngineError, EngineOOMError
from ..video_io import count_frames
from .base import AnchorMode, ChunkProgress, ChunkTask, Engine

_OOM_MARKERS = (
    "out of memory",
    "outofmemory",
    "cuda error",
    "allocation on device",
    "not enough memory",
)


def _looks_like_oom(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _OOM_MARKERS)


class WanAnimateEngine(Engine):
    name = "wan_animate"
    anchor_mode = AnchorMode.LATENT
    # Wan2.2-Animate 训练配置：77 帧窗口 / 5 帧 continue_motion
    native_window = 77
    native_overlap = 5

    def __init__(
        self,
        config: Optional[WanAnimateConfig] = None,
        output_dir: str = "./data/chunks",
    ) -> None:
        self.cfg = config or WanAnimateConfig()
        self.base_url = self.cfg.base_url.rstrip("/")
        self.client_id = f"scailswap-{uuid.uuid4().hex[:12]}"
        self.output_dir = output_dir
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # HTTP 基础
    # ------------------------------------------------------------------ #
    def _get(self, path: str, **kwargs) -> requests.Response:
        try:
            return self._session.get(
                f"{self.base_url}{path}", timeout=self.cfg.http_timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise EngineError(f"ComfyUI 请求失败 GET {path}: {exc}") from exc

    def _post(self, path: str, **kwargs) -> requests.Response:
        try:
            return self._session.post(
                f"{self.base_url}{path}", timeout=self.cfg.http_timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise EngineError(f"ComfyUI 请求失败 POST {path}: {exc}") from exc

    def health_check(self) -> dict:
        try:
            resp = self._get("/system_stats")
            resp.raise_for_status()
            return {
                "engine": self.name,
                "ok": True,
                "anchor_mode": self.anchor_mode.value,
                "window": self.native_window,
                "system_stats": resp.json().get("system", {}),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "engine": self.name,
                "ok": False,
                "anchor_mode": self.anchor_mode.value,
                "error": str(exc),
            }

    # ------------------------------------------------------------------ #
    # 显存管理
    # ------------------------------------------------------------------ #
    def free_memory(self, aggressive: bool = False) -> None:
        """调用 ComfyUI /free 释放显存（等价推理端 torch.cuda.empty_cache()）。

        aggressive=True 时同时卸载模型权重——本引擎一张图里要装
        Wan2.2-Animate 14B + SDPose + RT-DETR + SAM3 四个模型，OOM 时
        卸载重载比继续挤显存更稳。
        """
        try:
            self._post("/free", json={"unload_models": bool(aggressive), "free_memory": True})
        except EngineError:
            pass

    # ------------------------------------------------------------------ #
    # 文件上传 / 下载
    # ------------------------------------------------------------------ #
    def _upload(self, local_path: str, remote_name: str) -> str:
        with open(local_path, "rb") as fh:
            resp = self._post(
                "/upload/image",
                files={"image": (remote_name, fh, "application/octet-stream")},
                data={"overwrite": "true", "type": "input"},
            )
        if resp.status_code != 200:
            raise EngineError(f"上传失败 {local_path}: HTTP {resp.status_code} {resp.text[:300]}")
        payload = resp.json()
        name = payload.get("name") or remote_name
        sub = payload.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name

    def _download_output(self, file_info: dict, dest_path: str) -> str:
        params = {
            "filename": file_info["filename"],
            "subfolder": file_info.get("subfolder", ""),
            "type": file_info.get("type", "output"),
        }
        resp = self._get("/view", params=params, stream=True)
        if resp.status_code != 200:
            raise EngineError(f"下载输出失败：HTTP {resp.status_code}")
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
        with open(dest_path, "wb") as fh:
            for block in resp.iter_content(1 << 20):
                fh.write(block)
        return dest_path

    # ------------------------------------------------------------------ #
    # 工作流图构建
    # ------------------------------------------------------------------ #
    def _build_graph(
        self,
        task: ChunkTask,
        driving_name: str,
        reference_name: str,
        anchor_name: Optional[str],
    ) -> dict:
        cfg = self.cfg
        # mix=替换（保留原视频背景）；move=动作迁移（背景来自参考图）
        is_replacement = task.mode == "replacement"
        g: dict[str, dict] = {}

        def node(nid: str, class_type: str, **inputs) -> list:
            g[nid] = {"class_type": class_type, "inputs": inputs}
            return [nid, 0]

        # --- Wan2.2-Animate 主模型 + 加速/重打光 LoRA ---
        model = node("unet", "UNETLoader", unet_name=cfg.unet, weight_dtype=cfg.unet_weight_dtype)
        if cfg.lora_lightx2v:
            model = node(
                "lora_lightx2v", "LoraLoaderModelOnly",
                model=model, lora_name=cfg.lora_lightx2v,
                strength_model=cfg.lora_lightx2v_strength,
            )
        if is_replacement and cfg.lora_relight:
            # relight LoRA 是官方为替换模式训练的：让插入的角色与原场景光照色调一致
            model = node(
                "lora_relight", "LoraLoaderModelOnly",
                model=model, lora_name=cfg.lora_relight,
                strength_model=cfg.lora_relight_strength,
            )
        model = node("model_shift", "ModelSamplingSD3", model=model, shift=task.shift)

        # --- 文本条件 ---
        clip = node("clip", "CLIPLoader", clip_name=cfg.text_encoder, type="wan", device="default")
        pos = node("prompt_pos", "CLIPTextEncode", clip=clip, text=task.prompt)
        neg = node("prompt_neg", "CLIPTextEncode", clip=clip, text=task.negative_prompt)
        vae = node("vae", "VAELoader", vae_name=cfg.vae)

        # --- 参考角色图 + CLIP Vision ---
        ref_image = node("ref_image", "LoadImage", image=reference_name)
        clip_vision = node("clip_vision", "CLIPVisionLoader", clip_name=cfg.clip_vision)
        cv_out = node(
            "clip_vision_encode", "CLIPVisionEncode",
            clip_vision=clip_vision, image=ref_image, crop="none",
        )

        # --- 驱动视频分块 → 帧序列 ---
        drv_video = node("driving_video", "LoadVideo", file=driving_name)
        node("driving_frames", "GetVideoComponents", video=drv_video)
        drv_frames = ["driving_frames", 0]

        # --- 姿态 / 面部预处理（全原生）---
        # RT-DETR 先框出人体：SDPose 在有 bbox 时关键点更准，多人场景更是必需
        det_model = node(
            "rtdetr_model", "UNETLoader",
            unet_name=cfg.rtdetr_model, weight_dtype="default",
        )
        person_bboxes = node(
            "person_detect", "RTDETR_detect",
            model=det_model, image=drv_frames,
            threshold=float(task.extra.get("detection_threshold", 0.5)),
            class_name="person", max_detections=max(1, int(task.max_objects)),
        )
        node("sdpose_ckpt", "CheckpointLoaderSimple", ckpt_name=cfg.sdpose_model)
        keypoints = node(
            "pose_keypoints", "SDPoseKeypointExtractor",
            model=["sdpose_ckpt", 0], vae=["sdpose_ckpt", 2],
            image=drv_frames, batch_size=int(cfg.pose_batch_size),
            bboxes=person_bboxes,
        )
        # 骨架渲染 → pose_video（肢体控制信号）
        pose_video = node(
            "pose_render", "SDPoseDrawKeypoints",
            keypoints=keypoints, draw_body=True, draw_hands=True, draw_face=True,
            draw_feet=False, stick_width=4, face_point_size=3,
            score_threshold=0.3, draw_head=True,
        )
        # 人脸裁剪 → face_video（表情/口型信号，节点内部会缩到 512×512）
        face_bboxes = node(
            "face_bboxes", "SDPoseFaceBBoxes",
            keypoints=keypoints, scale=float(cfg.face_crop_scale), force_square=True,
        )
        face_video = node(
            "face_crops", "CropByBBoxes",
            image=drv_frames, bboxes=face_bboxes,
            output_width=512, output_height=512, padding=0, keep_aspect="stretch",
        )

        animate_inputs = dict(
            positive=pos, negative=neg, vae=vae,
            width=task.width, height=task.height,
            length=task.gen_length, batch_size=1,
            clip_vision_output=cv_out,
            reference_image=ref_image,
            face_video=face_video,
            pose_video=pose_video,
            continue_motion_max_frames=task.anchor_frames,
            video_frame_offset=0,
        )

        # --- 替换模式：原视频作背景 + SAM3 人物掩码限定重绘区域 ---
        if is_replacement:
            node("sam3_ckpt", "CheckpointLoaderSimple", ckpt_name=cfg.sam3_checkpoint)
            sam3_cond = node(
                "sam3_cond", "CLIPTextEncode",
                clip=["sam3_ckpt", 1], text=task.video_object,
            )
            track = node(
                "sam3_track", "SAM3_VideoTrack",
                images=drv_frames, model=["sam3_ckpt", 0], conditioning=sam3_cond,
                detection_threshold=float(task.extra.get("detection_threshold", 0.5)),
                max_objects=int(task.max_objects), detect_interval=1,
            )
            character_mask = node(
                "character_mask", "SAM3_TrackToMask",
                track_data=track,
                object_indices=str(task.extra.get("object_indices", "")),
            )
            animate_inputs["background_video"] = drv_frames
            animate_inputs["character_mask"] = character_mask

        # --- 长视频锚定：上一块输出送进 continue_motion ---
        if anchor_name:
            anchor_video = node("anchor_video", "LoadVideo", file=anchor_name)
            node("anchor_frames_node", "GetVideoComponents", video=anchor_video)
            animate_inputs["continue_motion"] = ["anchor_frames_node", 0]

        node("animate", "WanAnimateToVideo", **animate_inputs)

        # --- 采样 → 裁掉参考图 latent 头 → 解码 → 落盘 ---
        sampled = node(
            "sampler", "KSampler",
            model=model, seed=task.seed, steps=task.steps, cfg=task.cfg,
            sampler_name=cfg.sampler_name, scheduler=cfg.scheduler,
            positive=["animate", 0], negative=["animate", 1], latent_image=["animate", 2],
            denoise=1.0,
        )
        # trim_latent（animate 输出 3）：只裁掉 reference_image 占用的 latent 头。
        # continue_motion 帧已计入 length 窗口头部（noise_mask=0 冻结），解码后仍是
        # 完整 gen_length 帧——processor 要用重叠区做 crossfade，不能再裁。
        # 若误用 trim_image（输出 4）做 ImageFromBatch，会变成 77-5=72 触发帧数校验失败。
        trimmed = node(
            "trim_latent", "TrimVideoLatent",
            samples=sampled, trim_amount=["animate", 3],
        )
        decoded = node("decode", "VAEDecode", samples=trimmed, vae=vae)
        video_out = node("create_video", "CreateVideo", images=decoded, fps=task.fps)
        node(
            "save_video", "SaveVideo",
            video=video_out, filename_prefix=f"scailswap/wanani_{task.index:04d}",
            format="mp4", codec="h264",
        )
        return g

    # ------------------------------------------------------------------ #
    # 提交 + 轮询 + 下载
    # ------------------------------------------------------------------ #
    def generate_chunk(self, task: ChunkTask, on_progress: Optional[ChunkProgress] = None) -> str:
        def report(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        run_id = uuid.uuid4().hex[:8]
        report(0.02, "上传素材到推理端…")
        driving_name = self._upload(task.driving_video, f"wa_{run_id}_drv_{task.index:04d}.mp4")
        ref_ext = os.path.splitext(task.reference_image)[1] or ".png"
        reference_name = self._upload(task.reference_image, f"wa_{run_id}_ref{ref_ext}")
        anchor_name = None
        if task.anchor_video:
            anchor_name = self._upload(
                task.anchor_video, f"wa_{run_id}_anchor_{task.index:04d}.mp4"
            )

        graph = self._build_graph(task, driving_name, reference_name, anchor_name)
        report(0.05, "提交工作流…")
        resp = self._post("/prompt", json={"prompt": graph, "client_id": self.client_id})
        if resp.status_code != 200:
            detail = resp.text[:2000]
            if _looks_like_oom(detail):
                raise EngineOOMError(f"提交即 OOM：{detail}")
            raise EngineError(f"工作流提交被拒绝（HTTP {resp.status_code}）：{detail}")
        prompt_id = resp.json().get("prompt_id")
        if not prompt_id:
            raise EngineError(f"提交响应缺少 prompt_id：{resp.text[:500]}")

        output_info = self._wait_for_history(prompt_id, report)
        dest = os.path.join(self.output_dir, f"chunk_{task.index:04d}_{run_id}.mp4")
        report(0.95, "下载分块结果…")
        self._download_output(output_info, dest)

        got = count_frames(dest)
        if abs(got - task.gen_length) > 2:
            raise EngineError(
                f"分块 {task.index} 输出帧数异常：期望 {task.gen_length}，实际 {got}"
            )
        report(1.0, "分块完成")
        return dest

    def _wait_for_history(self, prompt_id: str, report) -> dict:
        deadline = time.time() + self.cfg.chunk_timeout
        while time.time() < deadline:
            hist_resp = self._get(f"/history/{prompt_id}")
            if hist_resp.status_code == 200:
                hist = hist_resp.json().get(prompt_id)
                if hist:
                    status = hist.get("status", {})
                    if status.get("status_str") == "error":
                        raw = json.dumps(status.get("messages", []), ensure_ascii=False)
                        if _looks_like_oom(raw):
                            raise EngineOOMError(f"推理 OOM：{raw[:1500]}")
                        raise EngineError(f"工作流执行失败：{raw[:2000]}")
                    if status.get("completed"):
                        return self._pick_video_output(hist.get("outputs", {}))
            queue_resp = self._get("/queue")
            if queue_resp.status_code == 200:
                q = queue_resp.json()
                pending = q.get("queue_pending", [])
                running = q.get("queue_running", [])
                pos = next(
                    (i + 1 for i, item in enumerate(pending)
                     if len(item) > 1 and item[1] == prompt_id),
                    None,
                )
                if pos:
                    report(0.08, f"GPU 排队中（第 {pos} 位）…")
                elif any(len(item) > 1 and item[1] == prompt_id for item in running):
                    report(0.5, "GPU 推理中（姿态提取 + 采样）…")
            time.sleep(self.cfg.poll_interval)
        try:
            self._post("/interrupt")
        except EngineError:
            pass
        raise EngineError(f"分块推理超时（>{self.cfg.chunk_timeout:.0f}s），已发送中断")

    @staticmethod
    def _pick_video_output(outputs: dict) -> dict:
        video_exts = (".mp4", ".webm", ".mov", ".mkv")
        for node_output in outputs.values():
            for value in node_output.values():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and str(
                        item.get("filename", "")
                    ).lower().endswith(video_exts):
                        return item
        raise EngineError(
            f"工作流完成但未找到视频输出：{json.dumps(outputs, ensure_ascii=False)[:800]}"
        )
