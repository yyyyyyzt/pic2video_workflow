"""RoleSwapClient —— 基础客户端。

职责：提交单段（≤5s）换脸任务、轮询结果、上传本地文件、下载输出视频。
完全屏蔽 ComfyUI 的画布 / 节点概念，对外只暴露语义化方法。
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import workflow_template as wf
from .config import RoleSwapConfig
from .log_utils import describe_input_ref, get_logger
from .upload_utils import (
    IMAGE_UPLOAD_CANDIDATES,
    VIDEO_UPLOAD_CANDIDATES,
    encode_as_data_uri,
    try_upload,
)

# 用于判断一个字符串输入是「公网 URL」「base64 data」还是「本地文件路径」。
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:", re.IGNORECASE)

logger = get_logger("roleswap.client")

PollCallback = Callable[[Dict[str, Any]], None]


class RoleSwapError(RuntimeError):
    """封装库统一异常类型。"""


class RoleSwapClient:
    """角色替换推理服务的基础客户端。

    Parameters
    ----------
    config:
        显式配置。若为 ``None`` 则从环境变量 / .env 读取（见 RoleSwapConfig）。
    session:
        可选的 requests.Session，便于连接复用 / 自定义适配器。
    """

    def __init__(
        self,
        config: Optional[RoleSwapConfig] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config or RoleSwapConfig.from_env()
        self.session = session or requests.Session()
        self._input_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        headers.update(self._auth_headers())
        return headers

    @staticmethod
    def _is_url(value: str) -> bool:
        return bool(_URL_RE.match(value))

    @staticmethod
    def _is_data_uri(value: str) -> bool:
        return bool(_DATA_URI_RE.match(value))

    def _resolve_input(self, value: str, *, kind: str) -> str:
        """把用户输入解析成后端可接受的引用。

        - 公网 URL / base64 data URI：原样透传。
        - 本地文件：按 ``input_mode`` 编码为 base64 或上传到服务器。
        """
        if self._is_url(value) or self._is_data_uri(value):
            return value
        if not os.path.exists(value):
            raise RoleSwapError(
                f"无法识别的 {kind} 输入：{value!r}。应为公网 URL、base64 data URI，"
                "或存在的本地文件路径。"
            )

        cache_key = f"{kind}:{os.path.abspath(value)}"
        if cache_key in self._input_cache:
            return self._input_cache[cache_key]

        resolved = self._materialize_local_file(value, kind=kind)
        logger.info(
            "解析本地 %s: %s -> %s",
            kind,
            value,
            describe_input_ref(resolved),
        )
        self._input_cache[cache_key] = resolved
        return resolved

    def _materialize_local_file(self, local_path: str, *, kind: str) -> str:
        mode = self.config.input_mode
        if mode == "base64":
            return self._encode_local_file(local_path, kind=kind)
        if mode == "upload":
            return self.upload_file(local_path, kind=kind)

        # auto：先尝试上传，失败则回退 base64
        try:
            return self.upload_file(local_path, kind=kind)
        except RoleSwapError as exc:
            if "405" in str(exc) or "Method Not Allowed" in str(exc):
                return self._encode_local_file(local_path, kind=kind)
            raise

    def _encode_local_file(self, local_path: str, *, kind: str) -> str:
        size = os.path.getsize(local_path)
        logger.info("base64 编码 %s: %d bytes (%.2f MB)", local_path, size, size / 1048576)
        if size > self.config.max_base64_bytes:
            raise RoleSwapError(
                f"文件 {local_path} 大小 {size} 字节，超过 base64 上限 "
                f"{self.config.max_base64_bytes}。请改用公网 URL，或配置可用的上传端点 "
                "(ROLESWAP_INPUT_MODE=upload)。"
            )
        return encode_as_data_uri(local_path, kind=kind)

    # ------------------------------------------------------------------ #
    # 文件上传
    # ------------------------------------------------------------------ #
    def upload_file(self, local_path: str, *, kind: str = "image") -> str:
        """上传本地文件到 ComfyUI 代理，返回服务器端文件引用。

        会依次尝试多个常见上传端点。若全部失败，请使用 ``input_mode=base64``。
        """
        if not os.path.exists(local_path):
            raise RoleSwapError(f"本地文件不存在：{local_path}")

        candidates = list(
            IMAGE_UPLOAD_CANDIDATES if kind == "image" else VIDEO_UPLOAD_CANDIDATES
        )
        # 用户自定义主端点优先
        custom = self.config.upload_path
        if custom:
            for field in ("image", "file", "video"):
                pair = (custom, field)
                if pair not in candidates:
                    candidates.insert(0, pair)

        ref, errors = try_upload(
            self.session,
            base_url=self.config.base_url,
            local_path=local_path,
            kind=kind,
            headers=self._auth_headers(),
            timeout=self.config.http_timeout,
            candidates=candidates,
        )
        if ref:
            return ref

        detail = "\n".join(errors[:6])
        raise RoleSwapError(
            f"上传失败：所有端点均不可用。最后错误：\n{detail}\n"
            "建议设置 ROLESWAP_INPUT_MODE=base64（API 原生支持 data URI）。"
        )

    # ------------------------------------------------------------------ #
    # 提交任务
    # ------------------------------------------------------------------ #
    def submit(
        self,
        video: str,
        face_image: str,
        steps: int = 6,
        cfg: float = 1.0,
        shift: float = 5.0,
        seed: Optional[int] = None,
        frame_load_cap: int = wf.FRAME_LOAD_CAP,
        *,
        options: Optional[wf.WorkflowOptions] = None,
        num_frames: Optional[int] = None,
    ) -> str:
        """提交单段换脸任务，返回 ``prompt_id``。

        Parameters
        ----------
        video:
            原始表演视频（公网 URL / base64 / 本地路径；本地路径将自动上传）。
        face_image:
            目标人脸照片（公网 URL / base64 / 本地路径；本地路径将自动上传）。
        steps, cfg, shift:
            采样相关参数（若提供 ``options`` 则以 options 为准）。
        seed:
            随机种子。为 ``None`` 时自动生成。长视频建议固定同一值。
        frame_load_cap:
            单次加载帧数上限（节点 125:value），默认 121。
        options:
            完整工作流可调参数（模式、提示词、强度等）。提供后会与 steps/cfg/shift
            合并，其中显式传入的 steps/cfg/shift 优先。
        num_frames:
            本次推理实际帧数（写入 125:value）。长视频分段时传入片段帧数。
        """
        if seed is None:
            seed = random.getrandbits(48)

        resolved_video = self._resolve_input(video, kind="video")
        resolved_image = self._resolve_input(face_image, kind="image")

        wf_opts = options or wf.WorkflowOptions()
        wf_opts.steps = steps
        wf_opts.cfg = cfg
        wf_opts.shift = shift
        wf_opts.seed = seed
        if num_frames is None:
            wf_opts.frame_load_cap = frame_load_cap
        else:
            wf_opts.frame_load_cap = num_frames

        payload = wf.build_payload(
            workflow_id=self.config.workflow_id,
            video=resolved_video,
            image=resolved_image,
            seed=seed,
            options=wf_opts,
            num_frames=num_frames or frame_load_cap,
        )

        url = self.config.url(self.config.submit_path)
        import json as _json

        payload_bytes = len(_json.dumps(payload, ensure_ascii=False))
        logger.info(
            "提交任务 -> %s | payload≈%.2fMB | video=%s | image=%s | "
            "num_frames=%s seed=%s steps=%s",
            url,
            payload_bytes / 1048576,
            describe_input_ref(resolved_video),
            describe_input_ref(resolved_image),
            num_frames or frame_load_cap,
            seed,
            steps,
        )

        try:
            resp = self.session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.config.submit_timeout,
            )
        except requests.RequestException as exc:
            logger.error("提交网络异常: %s", exc)
            raise RoleSwapError(f"提交网络异常：{exc}") from exc

        logger.debug("提交响应 status=%s body=%s", resp.status_code, resp.text[:2000])
        if resp.status_code >= 400:
            raise RoleSwapError(
                f"提交失败（{resp.status_code}）：{resp.text[:2000]}"
            )

        data = self._safe_json(resp)
        prompt_id = (
            data.get("prompt_id")
            or data.get("promptId")
            or data.get("id")
        )
        if not prompt_id:
            raise RoleSwapError(f"提交响应缺少 prompt_id：{data!r}")
        logger.info("提交成功 prompt_id=%s", prompt_id)
        return str(prompt_id)

    # ------------------------------------------------------------------ #
    # 轮询结果
    # ------------------------------------------------------------------ #
    def wait_for_result(
        self,
        prompt_id: str,
        timeout: Optional[int] = None,
        poll_interval: Optional[float] = None,
        on_poll: Optional[PollCallback] = None,
    ) -> str:
        """轮询任务结果，成功后返回输出视频的 URL。

        Parameters
        ----------
        prompt_id:
            submit 返回的任务 ID。
        timeout:
            最长等待秒数，默认取配置中的 result_timeout。
        poll_interval:
            轮询间隔秒数，默认取配置中的 poll_interval。
        """
        timeout = timeout if timeout is not None else self.config.result_timeout
        poll_interval = (
            poll_interval if poll_interval is not None else self.config.poll_interval
        )

        url = self.config.url(self.config.result_path)
        deadline = time.time() + timeout
        poll_count = 0
        last_body = ""

        while True:
            poll_count += 1
            try:
                resp = self.session.get(
                    url,
                    params={"prompt_id": prompt_id},
                    headers=self._headers(),
                    timeout=self.config.http_timeout,
                )
            except requests.RequestException as exc:
                logger.warning("轮询网络异常 prompt_id=%s: %s", prompt_id, exc)
                if time.time() >= deadline:
                    raise RoleSwapError(f"轮询网络异常：{exc}") from exc
                time.sleep(poll_interval)
                continue

            last_body = resp.text[:2000]
            if resp.status_code < 400:
                data = self._safe_json(resp)
                status = self._infer_poll_status(data)
                poll_info = self._build_poll_info(
                    prompt_id=prompt_id,
                    data=data,
                    status=status,
                    poll_count=poll_count,
                )
                if on_poll:
                    on_poll(poll_info)

                if poll_count == 1 or poll_count % 10 == 0:
                    logger.debug(
                        "轮询 #%d prompt_id=%s status=%s",
                        poll_count,
                        prompt_id,
                        status,
                    )

                if status == "failed":
                    raise RoleSwapError(
                        f"任务 {prompt_id} 失败：{data.get('error') or data!r}"
                    )

                output_url = self._extract_output_url(data)
                if output_url:
                    logger.info(
                        "任务完成 prompt_id=%s polls=%d url=%s",
                        prompt_id,
                        poll_count,
                        output_url[:200],
                    )
                    return output_url

                if status == "completed":
                    raise RoleSwapError(
                        self._format_missing_video_error(prompt_id, data)
                    )

                if status == "pending" and time.time() >= deadline:
                    deadline = time.time() + timeout
                    if poll_count % 10 == 0:
                        logger.info(
                            "任务仍在排队 prompt_id=%s，已延长等待（轮询 #%d）",
                            prompt_id,
                            poll_count,
                        )
            elif resp.status_code not in {202, 404, 425}:
                raise RoleSwapError(
                    f"查询结果失败（{resp.status_code}）：{resp.text[:2000]}"
                )

            if time.time() >= deadline:
                raise RoleSwapError(
                    f"任务 {prompt_id} 等待超时（>{timeout}s，轮询 {poll_count} 次）。"
                    f"最后响应：{last_body}"
                )
            time.sleep(poll_interval)

    # ------------------------------------------------------------------ #
    # 下载输出
    # ------------------------------------------------------------------ #
    def download(self, output_url: str, dest_path: str) -> str:
        """下载输出视频到本地路径，返回该路径。"""
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        candidates = self._candidate_download_urls(output_url)
        last_err: Optional[RoleSwapError] = None
        for idx, full_url in enumerate(candidates, start=1):
            logger.info(
                "下载输出 #%d/%d -> %s",
                idx,
                len(candidates),
                full_url[:200],
            )
            try:
                return self._download_once(full_url, dest_path)
            except RoleSwapError as exc:
                last_err = exc
                retryable = any(
                    token in str(exc)
                    for token in ("404", "502", "File not found", "not found")
                )
                if retryable and idx < len(candidates):
                    logger.warning("下载失败，尝试备用 URL：%s", exc)
                    continue
                raise
        if last_err:
            raise last_err
        raise RoleSwapError(f"无法下载输出：{output_url!r}")

    def _download_once(self, full_url: str, dest_path: str) -> str:
        try:
            with self.session.get(
                full_url,
                headers=self._headers(),
                stream=True,
                timeout=self.config.http_timeout,
            ) as resp:
                if resp.status_code >= 400:
                    raise RoleSwapError(
                        f"下载失败（{resp.status_code}）：{resp.text[:2000]}"
                    )
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
        except requests.RequestException as exc:
            raise RoleSwapError(f"下载网络异常：{exc}") from exc
        logger.info(
            "下载完成 %s (%.2f MB)",
            dest_path,
            os.path.getsize(dest_path) / 1048576,
        )
        return dest_path

    def _candidate_download_urls(self, output_url: str) -> list[str]:
        """生成一组候选下载 URL（主 URL + 路径修正变体）。"""
        primary = (
            output_url
            if self._is_url(output_url)
            else self.config.url(output_url)
        )
        primary = self._fix_view_url(primary)
        seen: set[str] = set()
        candidates: list[str] = []
        for url in (primary, output_url if self._is_url(output_url) else ""):
            if not url:
                continue
            fixed = self._fix_view_url(url)
            if fixed not in seen:
                seen.add(fixed)
                candidates.append(fixed)
        for alt in self._alternate_view_urls(primary):
            if alt not in seen:
                seen.add(alt)
                candidates.append(alt)
        return candidates or [primary]

    # ------------------------------------------------------------------ #
    # 响应解析辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_json(resp: requests.Response) -> Dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:  # 非 JSON 响应
            raise RoleSwapError(
                f"响应不是合法 JSON：{resp.text[:500]}"
            ) from exc
        if not isinstance(data, dict):
            return {"result": data}
        return data

    @staticmethod
    def _infer_poll_status(data: Dict[str, Any]) -> str:
        """从结果响应推断轮询状态（兼容 pending / running 等字段）。"""
        raw = str(data.get("status") or data.get("state") or "").lower()
        if raw in {"failed", "error"}:
            return "failed"
        if raw in {"completed", "success", "done"}:
            return "completed"
        if raw in {"running", "processing", "executing", "in_progress"}:
            return "running"
        if raw in {"queued", "in_queue", "waiting", "pending"}:
            return "pending"
        if data.get("pending") is True:
            return "pending"
        if data.get("pending") is False and data.get("success") is True:
            return "completed"
        if data.get("running") is True or data.get("executing") is True:
            return "running"
        progress = data.get("progress")
        if isinstance(progress, (int, float)) and 0 < float(progress) < 1:
            return "running"
        results = data.get("results")
        if (
            data.get("success") is True
            and isinstance(results, list)
            and len(results) == 0
            and not raw
        ):
            return "pending"
        return raw or "unknown"

    def _build_poll_info(
        self,
        *,
        prompt_id: str,
        data: Dict[str, Any],
        status: str,
        poll_count: int,
    ) -> Dict[str, Any]:
        queue_hint = None
        if poll_count % 10 == 0:
            queue_hint = self._lookup_queue_hint(prompt_id)
        detail = self._format_poll_detail(
            data=data,
            status=status,
            poll_count=poll_count,
            queue_hint=queue_hint,
        )
        return {
            "prompt_id": prompt_id,
            "status": status,
            "poll_count": poll_count,
            "detail": detail,
            "queue_hint": queue_hint,
            "progress": data.get("progress"),
            "current_node": data.get("current_node")
            or data.get("executing_node")
            or data.get("node"),
        }

    @staticmethod
    def _format_poll_detail(
        *,
        data: Dict[str, Any],
        status: str,
        poll_count: int,
        queue_hint: Optional[str] = None,
    ) -> str:
        label = {
            "pending": "GPU 排队中",
            "running": "GPU 推理中",
            "unknown": "等待远程响应",
            "completed": "已完成",
            "failed": "失败",
        }.get(status, status)
        parts = [label]
        if queue_hint:
            parts.append(queue_hint)
        progress = data.get("progress")
        if isinstance(progress, (int, float)):
            pct = float(progress) * 100 if float(progress) <= 1 else float(progress)
            if pct > 0:
                parts.append(f"进度 {pct:.0f}%")
        for key, label_name in (
            ("queue_position", "队列位置"),
            ("queue_remaining", "前方任务"),
            ("current_node", "节点"),
            ("executing_node", "节点"),
        ):
            val = data.get(key)
            if val is not None and str(val) != "":
                parts.append(f"{label_name} {val}")
        node = data.get("node")
        if node is not None and "节点" not in " ".join(parts):
            parts.append(f"节点 {node}")
        parts.append(f"轮询 #{poll_count}")
        return "，".join(parts)

    def _lookup_queue_hint(self, prompt_id: str) -> Optional[str]:
        """尽力查询 ComfyUI 队列，补充排队信息（接口不存在时静默跳过）。"""
        if not self.config.queue_path:
            return None
        try:
            resp = self.session.get(
                self.config.url(self.config.queue_path),
                headers=self._headers(),
                timeout=min(self.config.http_timeout, 15.0),
            )
        except requests.RequestException:
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        return self._parse_queue_for_prompt(data, prompt_id)

    @staticmethod
    def _parse_queue_for_prompt(data: Any, prompt_id: str) -> Optional[str]:
        """从 ComfyUI /queue 响应中查找 prompt 位置。"""
        if not isinstance(data, dict):
            return None
        running = data.get("queue_running") or data.get("running") or []
        pending = data.get("queue_pending") or data.get("pending") or []
        if isinstance(running, list):
            for idx, item in enumerate(running):
                if RoleSwapClient._queue_item_matches(item, prompt_id):
                    return f"正在执行（队列运行 #{idx + 1}）"
        if isinstance(pending, list):
            for idx, item in enumerate(pending):
                if RoleSwapClient._queue_item_matches(item, prompt_id):
                    return f"排队第 {idx + 1} 位（共 {len(pending)} 个待执行）"
        return None

    @staticmethod
    def _queue_item_matches(item: Any, prompt_id: str) -> bool:
        if isinstance(item, (list, tuple)):
            for part in item:
                if str(part) == prompt_id:
                    return True
            if item and str(item[0]) == prompt_id:
                return True
        if isinstance(item, dict):
            if str(item.get("prompt_id") or item.get("id") or "") == prompt_id:
                return True
        return prompt_id in str(item)

    def _extract_output_url(self, data: Dict[str, Any]) -> Optional[str]:
        """从结果响应中尽力提取输出视频 URL。

        兼容：
        - 顶层 url / video_url 字段
        - ComfyUI history：outputs -> 节点62(VHS_VideoCombine) -> gifs
        - 简化 API 包装的 results[] / output / results 字段
        """
        results = data.get("results")
        if isinstance(results, list):
            url = self._pick_video_from_results(results)
            if url:
                return url

        for key in ("video_url", "output_url", "url", "result_url"):
            val = data.get(key)
            if isinstance(val, str) and val and self._looks_like_video_ref(val):
                return self._normalize_output_ref(val)

        outputs = data.get("outputs")
        if isinstance(outputs, dict):
            node_order = ["62"] + [k for k in outputs if k != "62"]
            for node_id in node_order:
                url = self._extract_from_node_output(outputs.get(node_id))
                if url:
                    return url
        elif isinstance(outputs, list):
            url = self._pick_video_from_results(outputs)
            if url:
                return url

        for key in ("output", "result"):
            val = data.get(key)
            url = self._dig_url(val)
            if url:
                return self._normalize_output_ref(url)

        return None

    def _pick_video_from_results(self, results: List[Any]) -> Optional[str]:
        """从 API ``results`` 数组中挑选最终视频输出（忽略 temp 预览图）。"""
        ranked: list[tuple[int, str]] = []
        for item in results:
            parsed = self._parse_result_item(item)
            if not parsed:
                continue
            url, score = parsed
            ranked.append((score, url))
        if not ranked:
            return None
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0][1]

    def _parse_result_item(self, item: Any) -> Optional[tuple[str, int]]:
        """解析单条 result，返回 (url, priority)。非视频或 temp 文件返回 None。"""
        if isinstance(item, str):
            if self._looks_like_video_ref(item) and "temp" not in item.lower():
                return self._normalize_output_ref(item), 10
            return None
        if not isinstance(item, dict):
            return None

        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        media_type = str(item.get("type") or raw.get("type") or "").lower()
        ftype = str(raw.get("type") or item.get("ftype") or "").lower()
        filename = str(
            raw.get("filename") or item.get("filename") or item.get("name") or ""
        )
        url = str(item.get("url") or item.get("video_url") or item.get("output_url") or "")

        if ftype == "temp" or "ComfyUI_temp_" in filename:
            return None
        if media_type == "image" and not self._looks_like_video_ref(filename or url):
            return None

        score = 0
        if media_type in {"video", "gif", "gifs"}:
            score += 20
        if ftype == "output":
            score += 15
        if self._looks_like_video_ref(filename or url):
            score += 10
        if score <= 0:
            return None

        if url:
            return self._normalize_output_ref(url), score
        if filename:
            return (
                self._build_view_url(
                    filename=filename,
                    subfolder=str(raw.get("subfolder") or item.get("subfolder") or ""),
                    ftype=ftype or "output",
                ),
                score,
            )
        return None

    @staticmethod
    def _looks_like_video_ref(ref: str) -> bool:
        ref_l = ref.lower()
        return ref_l.endswith((".mp4", ".webm", ".mov", ".mkv", ".gif"))

    def _format_missing_video_error(self, prompt_id: str, data: Dict[str, Any]) -> str:
        """工作流已结束但未产出视频时的可读错误。"""
        artifacts = self._summarize_result_artifacts(data.get("results"))
        lines = [
            f"任务 {prompt_id} 已结束，但未生成视频输出。",
            "这通常表示 ComfyUI 工作流在中间节点失败，或视频合成节点（62）未执行。",
        ]
        if artifacts:
            lines.append(f"API 返回的附件：{artifacts}")
            if any("temp" in a.lower() or ".png" in a.lower() for a in artifacts):
                lines.append(
                    "当前仅有临时预览图（ComfyUI_temp_*.png），说明推理未走到最终视频导出。"
                    "请在 ComfyUI 画布查看该 prompt 的执行日志与报错节点。"
                )
        else:
            lines.append(f"原始响应：{data!r}")
        lines.append(
            "建议：在 ComfyUI 手动跑同一段输入，确认节点 62（VHS_VideoCombine）能正常输出 mp4。"
        )
        return "\n".join(lines)

    @staticmethod
    def _summarize_result_artifacts(results: Any) -> List[str]:
        if not isinstance(results, list):
            return []
        out: List[str] = []
        for item in results:
            if isinstance(item, dict):
                raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
                name = raw.get("filename") or item.get("filename") or item.get("type")
                ftype = raw.get("type") or item.get("type")
                if name:
                    out.append(f"{name} (type={ftype})")
            elif item:
                out.append(str(item))
        return out

    def _extract_from_node_output(self, node_out: Any) -> Optional[str]:
        if not isinstance(node_out, dict):
            return None
        for key in ("gifs", "videos", "images"):
            items = node_out.get(key)
            if isinstance(items, list):
                for item in items:
                    url = self._media_item_to_url(item)
                    if url:
                        return url
        return self._dig_url(node_out)

    def _media_item_to_url(self, item: Any) -> Optional[str]:
        if isinstance(item, str):
            if not self._looks_like_video_ref(item):
                return None
            return self._normalize_output_ref(item)
        if isinstance(item, dict):
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            ftype = str(raw.get("type") or item.get("type") or "output").lower()
            filename = str(
                item.get("filename") or item.get("name") or raw.get("filename") or ""
            )
            if ftype == "temp" or "ComfyUI_temp_" in filename:
                return None
            if str(item.get("type") or "").lower() == "image" and not self._looks_like_video_ref(
                filename
            ):
                return None
            for key in ("url", "video_url", "output_url", "fullpath"):
                val = item.get(key)
                if isinstance(val, str) and val and self._looks_like_video_ref(val):
                    return self._normalize_output_ref(val)
            if filename and self._looks_like_video_ref(filename):
                return self._build_view_url(
                    filename=filename,
                    subfolder=str(item.get("subfolder") or raw.get("subfolder") or ""),
                    ftype=ftype or "output",
                )
        return None

    def _build_view_url(self, *, filename: str, subfolder: str, ftype: str) -> str:
        filename, subfolder, ftype = self._split_comfy_output_path(
            filename, subfolder=subfolder, ftype=ftype
        )
        params = {"filename": filename, "type": ftype}
        if subfolder:
            params["subfolder"] = subfolder
        return f"{self.config.url(self.config.view_path)}?{urlencode(params)}"

    @staticmethod
    def _split_comfy_output_path(
        ref: str,
        *,
        subfolder: str = "",
        ftype: str = "output",
    ) -> tuple[str, str, str]:
        """把 ``/output/Scail2/foo.mp4`` 拆成 ComfyUI view 参数。"""
        path = ref.replace("\\", "/").strip()
        if path.startswith("/"):
            path = path.lstrip("/")
        if path.lower().startswith("output/"):
            path = path[7:]
        if subfolder:
            subfolder = subfolder.replace("\\", "/").strip("/")
            if subfolder.lower().startswith("output/"):
                subfolder = subfolder[7:]
        if "/" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2:
                return parts[-1], "/".join(parts[:-1]), ftype
        return path, subfolder, ftype

    def _fix_view_url(self, url: str) -> str:
        """修正 view URL 中 filename 误带 ``/output/`` 前缀的情况。"""
        if not self._is_url(url):
            return url
        parsed = urlparse(url)
        if self.config.view_path.lstrip("/") not in parsed.path:
            return url
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filename = (qs.get("filename") or [""])[0]
        if not filename:
            return url
        subfolder = (qs.get("subfolder") or [""])[0]
        ftype = (qs.get("type") or ["output"])[0]
        fixed_filename, fixed_subfolder, fixed_type = self._split_comfy_output_path(
            filename,
            subfolder=subfolder,
            ftype=ftype,
        )
        return self._build_view_url(
            filename=fixed_filename,
            subfolder=fixed_subfolder,
            ftype=fixed_type,
        )

    def _alternate_view_urls(self, url: str) -> list[str]:
        """为同一输出文件生成若干备用 view URL。"""
        if not self._is_url(url):
            return []
        parsed = urlparse(url)
        if self.config.view_path.lstrip("/") not in parsed.path:
            return []
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filename = (qs.get("filename") or [""])[0]
        if not filename:
            return []
        subfolder = (qs.get("subfolder") or [""])[0]
        ftype = (qs.get("type") or ["output"])[0]
        base_name, derived_subfolder, _ = self._split_comfy_output_path(filename)
        variants: list[tuple[str, str, str]] = []
        if derived_subfolder:
            variants.append((base_name, derived_subfolder, ftype))
        if subfolder and subfolder != derived_subfolder:
            variants.append((base_name, subfolder, ftype))
        variants.append((base_name, "", ftype))
        if derived_subfolder:
            variants.append((f"{derived_subfolder}/{base_name}", "", ftype))
        alternates: list[str] = []
        for fn, sub, ft in variants:
            alt = self._build_view_url(filename=fn, subfolder=sub, ftype=ft)
            if alt != url:
                alternates.append(alt)
        return alternates

    def _normalize_output_ref(self, ref: str) -> str:
        if self._is_url(ref):
            return self._fix_view_url(ref)
        if self._looks_like_video_ref(ref):
            filename, subfolder, ftype = self._split_comfy_output_path(
                ref.lstrip("/")
            )
            return self._build_view_url(
                filename=filename,
                subfolder=subfolder,
                ftype=ftype,
            )
        if ref.startswith("/"):
            return self._fix_view_url(self.config.url(ref))
        return ref

    @staticmethod
    def _dig_url(val: Any) -> Optional[str]:
        """递归地从嵌套结构里找出看起来像视频 URL / 文件名的字符串。"""
        if val is None:
            return None
        if isinstance(val, str):
            if val.startswith("http") or val.lower().endswith(
                (".mp4", ".webm", ".mov", ".mkv")
            ):
                return val
            return None
        if isinstance(val, dict):
            for key in ("video_url", "output_url", "url", "filename", "name", "fullpath"):
                if key in val:
                    found = RoleSwapClient._dig_url(val[key])
                    if found:
                        return found
            for sub in val.values():
                found = RoleSwapClient._dig_url(sub)
                if found:
                    return found
            return None
        if isinstance(val, (list, tuple)):
            for item in val:
                found = RoleSwapClient._dig_url(item)
                if found:
                    return found
        return None
