"""RoleSwapClient —— 基础客户端。

职责：提交单段（≤5s）换脸任务、轮询结果、上传本地文件、下载输出视频。
完全屏蔽 ComfyUI 的画布 / 节点概念，对外只暴露语义化方法。
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Dict, Optional

import requests

from .config import RoleSwapConfig
from . import workflow_template as wf


# 用于判断一个字符串输入是「公网 URL」「base64 data」还是「本地文件路径」。
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:", re.IGNORECASE)


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

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
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
        - 本地文件：先上传到服务器，返回服务器端文件名。
        """
        if self._is_url(value) or self._is_data_uri(value):
            return value
        if os.path.exists(value):
            return self.upload_file(value)
        raise RoleSwapError(
            f"无法识别的 {kind} 输入：{value!r}。应为公网 URL、base64 data URI，"
            "或存在的本地文件路径。"
        )

    # ------------------------------------------------------------------ #
    # 文件上传
    # ------------------------------------------------------------------ #
    def upload_file(self, local_path: str) -> str:
        """上传本地文件到服务器（/api/comfy/upload/file），返回服务器端文件名。"""
        if not os.path.exists(local_path):
            raise RoleSwapError(f"本地文件不存在：{local_path}")

        url = self.config.url(self.config.upload_path)
        with open(local_path, "rb") as fh:
            files = {"image": (os.path.basename(local_path), fh)}
            resp = self.session.post(
                url,
                files=files,
                headers=self._headers(),
                timeout=self.config.http_timeout,
            )
        if resp.status_code >= 400:
            raise RoleSwapError(
                f"上传失败（{resp.status_code}）：{resp.text[:500]}"
            )

        data = self._safe_json(resp)
        # ComfyUI upload 通常返回 {"name": "...", "subfolder": "...", "type": "..."}
        name = data.get("name") or data.get("filename")
        if not name:
            raise RoleSwapError(f"上传响应缺少文件名字段：{data!r}")
        subfolder = data.get("subfolder")
        return f"{subfolder}/{name}" if subfolder else name

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
    ) -> str:
        """提交单段换脸任务，返回 ``prompt_id``。

        Parameters
        ----------
        video:
            原始表演视频（公网 URL / base64 / 本地路径；本地路径将自动上传）。
        face_image:
            目标人脸照片（公网 URL / base64 / 本地路径；本地路径将自动上传）。
        steps, cfg, shift:
            采样相关参数，见 workflow_template.build_payload。
        seed:
            随机种子。为 ``None`` 时自动生成。长视频建议固定同一值。
        frame_load_cap:
            单次加载帧数上限，默认 121（工作流硬上限）。
        """
        if seed is None:
            seed = random.randint(0, 2**32 - 1)

        resolved_video = self._resolve_input(video, kind="video")
        resolved_image = self._resolve_input(face_image, kind="image")

        payload = wf.build_payload(
            workflow_id=self.config.workflow_id,
            video=resolved_video,
            image=resolved_image,
            steps=steps,
            cfg=cfg,
            shift=shift,
            seed=seed,
            frame_load_cap=frame_load_cap,
        )

        url = self.config.url(self.config.submit_path)
        resp = self.session.post(
            url,
            json=payload,
            headers=self._headers(),
            timeout=self.config.http_timeout,
        )
        if resp.status_code >= 400:
            raise RoleSwapError(
                f"提交失败（{resp.status_code}）：{resp.text[:500]}"
            )

        data = self._safe_json(resp)
        prompt_id = (
            data.get("prompt_id")
            or data.get("promptId")
            or data.get("id")
        )
        if not prompt_id:
            raise RoleSwapError(f"提交响应缺少 prompt_id：{data!r}")
        return str(prompt_id)

    # ------------------------------------------------------------------ #
    # 轮询结果
    # ------------------------------------------------------------------ #
    def wait_for_result(
        self,
        prompt_id: str,
        timeout: Optional[int] = None,
        poll_interval: Optional[float] = None,
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

        while True:
            resp = self.session.get(
                url,
                params={"prompt_id": prompt_id},
                headers=self._headers(),
                timeout=self.config.http_timeout,
            )
            if resp.status_code < 400:
                data = self._safe_json(resp)
                status = str(
                    data.get("status") or data.get("state") or ""
                ).lower()

                if status in {"failed", "error"}:
                    raise RoleSwapError(
                        f"任务 {prompt_id} 失败：{data.get('error') or data!r}"
                    )

                output_url = self._extract_output_url(data)
                if output_url:
                    return output_url

                # 已完成但没解析到 URL —— 视为异常，避免死循环。
                if status in {"completed", "success", "done"}:
                    raise RoleSwapError(
                        f"任务 {prompt_id} 显示完成但未找到输出 URL：{data!r}"
                    )
            elif resp.status_code not in {202, 404, 425}:
                # 202/404/425 视为「还没就绪」，其余状态码视为错误。
                raise RoleSwapError(
                    f"查询结果失败（{resp.status_code}）：{resp.text[:500]}"
                )

            if time.time() >= deadline:
                raise RoleSwapError(
                    f"任务 {prompt_id} 等待超时（>{timeout}s）。"
                )
            time.sleep(poll_interval)

    # ------------------------------------------------------------------ #
    # 下载输出
    # ------------------------------------------------------------------ #
    def download(self, output_url: str, dest_path: str) -> str:
        """下载输出视频到本地路径，返回该路径。"""
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        # 支持相对 URL（后端可能只返回路径）
        full_url = output_url if self._is_url(output_url) else self.config.url(output_url)
        with self.session.get(
            full_url,
            headers=self._headers(),
            stream=True,
            timeout=self.config.http_timeout,
        ) as resp:
            if resp.status_code >= 400:
                raise RoleSwapError(
                    f"下载失败（{resp.status_code}）：{resp.text[:500]}"
                )
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
        return dest_path

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
    def _extract_output_url(data: Dict[str, Any]) -> Optional[str]:
        """从结果响应中尽力提取输出视频 URL。

        兼容多种常见返回结构：顶层 url / output / result，或嵌套的
        outputs -> [ {url|filename} ] 等形式。
        """
        # 1) 直接字段
        for key in ("video_url", "output_url", "url", "result_url"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val

        # 2) output / result 为字符串或列表 / 字典
        for key in ("output", "result", "outputs", "results"):
            val = data.get(key)
            url = RoleSwapClient._dig_url(val)
            if url:
                return url

        return None

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
            for key in ("video_url", "output_url", "url", "filename", "name"):
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
