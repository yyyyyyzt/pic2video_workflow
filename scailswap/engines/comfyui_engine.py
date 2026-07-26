"""自托管 ComfyUI 引擎（主路线）。

用 ComfyUI **原生节点**（无需第三方自定义节点）逐块构建 SCAIL-2 工作流图：

    UNETLoader(wan2.1_14B_SCAIL_2) ── LoRA(lightx2v蒸馏) ── LoRA(DPO) ── ModelSamplingSD3
    CLIPLoader(umt5_xxl) → CLIPTextEncode(正/负向提示词)
    LoadVideo(驱动分块) → GetVideoComponents → SAM3_VideoTrack ─┐
    LoadImage(参考图)  → SAM3_Detect ──────────────────────────┤
                                                    SCAIL2ColoredMask（按身份着色的掩码）
    LoadVideo(锚点=上一块输出) → GetVideoComponents ────────────┐
                                                               ▼
    WanSCAILToVideo(pose_video, 掩码, 参考图, clip_vision, previous_frames←锚点)
        → KSampler → VAEDecode → CreateVideo → SaveVideo

长视频时间一致性的关键在 ``previous_frames``：WanSCAILToVideo 会取锚点视频的
末尾 ``previous_frame_count``（默认 5）帧，经 VAE 编码后**冻结**为新块 latent 的
头部（noise_mask=0，不加噪不重采样），模型在"已知开头"的条件下续写剩余帧。
身份、服装、光影、动作速度的衔接由模型语义保证，而非事后拼接。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

import requests

from ..config import ComfyUIConfig
from ..errors import EngineError, EngineOOMError
from ..video_io import count_frames
from .base import ChunkProgress, ChunkTask, Engine

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


class ComfyUIEngine(Engine):
    name = "comfyui"
    supports_anchor = True  # 原生 previous_frames 锚定

    def __init__(self, config: Optional[ComfyUIConfig] = None, output_dir: str = "./data/chunks"):
        self.cfg = config or ComfyUIConfig()
        self.base_url = self.cfg.base_url.rstrip("/")
        self.client_id = f"scailswap-{uuid.uuid4().hex[:12]}"
        self.output_dir = output_dir
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # HTTP 基础
    # ------------------------------------------------------------------ #
    def _get(self, path: str, **kwargs) -> requests.Response:
        try:
            resp = self._session.get(
                f"{self.base_url}{path}", timeout=self.cfg.http_timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise EngineError(f"ComfyUI 请求失败 GET {path}: {exc}") from exc
        return resp

    def _post(self, path: str, **kwargs) -> requests.Response:
        try:
            resp = self._session.post(
                f"{self.base_url}{path}", timeout=self.cfg.http_timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise EngineError(f"ComfyUI 请求失败 POST {path}: {exc}") from exc
        return resp

    def health_check(self) -> dict:
        try:
            resp = self._get("/system_stats")
            resp.raise_for_status()
            stats = resp.json()
            return {"engine": self.name, "ok": True, "system_stats": stats.get("system", {})}
        except Exception as exc:  # noqa: BLE001
            return {"engine": self.name, "ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ #
    # 显存管理（Phase 4）
    # ------------------------------------------------------------------ #
    def free_memory(self, aggressive: bool = False) -> None:
        """调用 ComfyUI /free 释放显存。

        - free_memory=True 等价于在推理端执行 torch.cuda.empty_cache()；
        - aggressive=True 时同时卸载模型权重（OOM 重试前的兜底手段）。
        """
        try:
            self._post(
                "/free",
                json={"unload_models": bool(aggressive), "free_memory": True},
            )
        except EngineError:
            # 清显存失败不阻断主流程（可能是老版本 ComfyUI 无 /free）
            pass

    # ------------------------------------------------------------------ #
    # 文件上传 / 下载
    # ------------------------------------------------------------------ #
    def _upload(self, local_path: str, remote_name: str) -> str:
        """把本地文件上传到 ComfyUI 的 input 目录，返回其 input 文件名。"""
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
        """构建单块生成的 API 格式节点图。节点 id 用语义化字符串便于排错。"""
        cfg = self.cfg
        replacement = task.mode == "replacement"
        g: dict[str, dict] = {}

        def node(nid: str, class_type: str, **inputs) -> list:
            g[nid] = {"class_type": class_type, "inputs": inputs}
            return [nid, 0]

        # --- 扩散模型 + 蒸馏/DPO LoRA + 时序 shift ---
        model = node("unet", "UNETLoader", unet_name=cfg.unet, weight_dtype=cfg.unet_weight_dtype)
        if cfg.lora_lightx2v:
            model = node(
                "lora_lightx2v", "LoraLoaderModelOnly",
                model=model, lora_name=cfg.lora_lightx2v,
                strength_model=cfg.lora_lightx2v_strength,
            )
        if cfg.lora_dpo:
            # DPO LoRA：官方后训练权重，改善手部细节与口型/眼神同步
            model = node(
                "lora_dpo", "LoraLoaderModelOnly",
                model=model, lora_name=cfg.lora_dpo,
                strength_model=cfg.lora_dpo_strength,
            )
        model = node("model_shift", "ModelSamplingSD3", model=model, shift=task.shift)

        # --- 文本条件 ---
        clip = node("clip", "CLIPLoader", clip_name=cfg.text_encoder, type="wan", device="default")
        pos = node("prompt_pos", "CLIPTextEncode", clip=clip, text=task.prompt)
        neg = node("prompt_neg", "CLIPTextEncode", clip=clip, text=task.negative_prompt)

        vae = node("vae", "VAELoader", vae_name=cfg.vae)

        # --- 参考图 + CLIP Vision ---
        ref_image = node("ref_image", "LoadImage", image=reference_name)
        clip_vision = node("clip_vision", "CLIPVisionLoader", clip_name=cfg.clip_vision)
        cv_out = node(
            "clip_vision_encode", "CLIPVisionEncode",
            clip_vision=clip_vision, image=ref_image, crop="none",
        )

        # --- 驱动视频分块 ---
        drv_video = node("driving_video", "LoadVideo", file=driving_name)
        node("driving_frames", "GetVideoComponents", video=drv_video)
        drv_frames = ["driving_frames", 0]

        # --- SAM3 跟踪 → 按身份着色的掩码（SCAIL-2 的核心条件输入之一）---
        node("sam3_ckpt", "CheckpointLoaderSimple", ckpt_name=cfg.sam3_checkpoint)
        sam3_model = ["sam3_ckpt", 0]
        sam3_clip = ["sam3_ckpt", 1]
        sam3_cond_video = node(
            "sam3_cond_video", "CLIPTextEncode", clip=sam3_clip, text=task.video_object
        )
        sam3_cond_image = node(
            "sam3_cond_image", "CLIPTextEncode", clip=sam3_clip, text=task.image_object
        )
        track = node(
            "sam3_track", "SAM3_VideoTrack",
            images=drv_frames, model=sam3_model, conditioning=sam3_cond_video,
            detection_threshold=float(task.extra.get("detection_threshold", 0.5)),
            max_objects=int(task.max_objects), detect_interval=1,
        )
        node(
            "sam3_ref_detect", "SAM3_Detect",
            model=sam3_model, image=ref_image, conditioning=sam3_cond_image,
            threshold=float(task.extra.get("detection_threshold", 0.5)),
            refine_iterations=2, individual_masks=False,
        )
        ref_mask = ["sam3_ref_detect", 0]
        node(
            "colored_mask", "SCAIL2ColoredMask",
            driving_track_data=track, ref_track_data=ref_mask,
            object_indices=str(task.extra.get("object_indices", "")),
            sort_by="left_to_right", replacement_mode=replacement,
        )
        pose_mask = ["colored_mask", 0]
        ref_colored_mask = ["colored_mask", 1]

        # --- SCAIL-2 条件组装（含长视频锚定）---
        scail_inputs = dict(
            positive=pos, negative=neg, vae=vae,
            width=task.width, height=task.height,
            length=task.gen_length, batch_size=1,
            pose_video=drv_frames, pose_video_mask=pose_mask,
            replacement_mode=replacement,
            pose_strength=float(task.extra.get("pose_strength", 1.0)),
            pose_start=0.0, pose_end=1.0,
            reference_image=ref_image, reference_image_mask=ref_colored_mask,
            clip_vision_output=cv_out,
            video_frame_offset=0,
            previous_frame_count=task.anchor_frames,
        )
        if anchor_name:
            # 锚点：上一块的完整输出。节点内部取末尾 previous_frame_count 帧
            # → VAE 编码 → 冻结为新块 latent 头部（模型级语义衔接的实现点）。
            anchor_video = node("anchor_video", "LoadVideo", file=anchor_name)
            node("anchor_frames", "GetVideoComponents", video=anchor_video)
            scail_inputs["previous_frames"] = ["anchor_frames", 0]
        node("scail", "WanSCAILToVideo", **scail_inputs)

        # --- 采样 / 解码 / 落盘 ---
        sampled = node(
            "sampler", "KSampler",
            model=model, seed=task.seed, steps=task.steps, cfg=task.cfg,
            sampler_name="euler", scheduler="simple",
            positive=["scail", 0], negative=["scail", 1], latent_image=["scail", 2],
            denoise=1.0,
        )
        decoded = node("decode", "VAEDecode", samples=sampled, vae=vae)
        video_out = node("create_video", "CreateVideo", images=decoded, fps=task.fps)
        node(
            "save_video", "SaveVideo",
            video=video_out, filename_prefix=f"scailswap/chunk_{task.index:04d}",
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
        driving_name = self._upload(task.driving_video, f"ss_{run_id}_drv_{task.index:04d}.mp4")
        reference_name = self._upload(task.reference_image, f"ss_{run_id}_ref{os.path.splitext(task.reference_image)[1] or '.png'}")
        anchor_name = None
        if task.anchor_video:
            anchor_name = self._upload(task.anchor_video, f"ss_{run_id}_anchor_{task.index:04d}.mp4")

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
        if got != task.gen_length:
            # H.264 封装偶尔多/少 1 帧属于容器层问题；差距大说明工作流异常
            if abs(got - task.gen_length) > 2:
                raise EngineError(
                    f"分块 {task.index} 输出帧数异常：期望 {task.gen_length}，实际 {got}"
                )
        report(1.0, "分块完成")
        return dest

    def _wait_for_history(self, prompt_id: str, report) -> dict:
        """轮询 /history 直到任务完成，返回输出视频的文件描述。"""
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
            # 未完成：汇报队列位置
            queue_resp = self._get("/queue")
            if queue_resp.status_code == 200:
                q = queue_resp.json()
                pending = q.get("queue_pending", [])
                running = q.get("queue_running", [])
                pos = next(
                    (i + 1 for i, item in enumerate(pending) if len(item) > 1 and item[1] == prompt_id),
                    None,
                )
                if pos:
                    report(0.08, f"GPU 排队中（第 {pos} 位）…")
                elif any(len(item) > 1 and item[1] == prompt_id for item in running):
                    report(0.5, "GPU 推理中…")
            time.sleep(self.cfg.poll_interval)
        # 超时：中断远端任务避免占卡
        try:
            self._post("/interrupt")
        except EngineError:
            pass
        raise EngineError(f"分块推理超时（>{self.cfg.chunk_timeout:.0f}s），已发送中断")

    @staticmethod
    def _pick_video_output(outputs: dict) -> dict:
        """在 history outputs 里找视频文件（SaveVideo 的输出键随版本变化，做兼容扫描）。"""
        video_exts = (".mp4", ".webm", ".mov", ".mkv")
        for node_output in outputs.values():
            for value in node_output.values():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(video_exts):
                        return item
        raise EngineError(f"工作流完成但未找到视频输出：{json.dumps(outputs, ensure_ascii=False)[:800]}")
