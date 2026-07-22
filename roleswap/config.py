"""配置模块：所有环境相关设置从环境变量 / .env 读取，绝不硬编码。"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    # python-dotenv 为可选依赖：存在则自动加载同目录 / 上层的 .env 文件
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv 未安装时优雅降级
    pass


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class RoleSwapConfig:
    """封装推理服务的连接配置。

    默认从以下环境变量读取（见 .env.example）：

    - ``ROLESWAP_BASE_URL``      推理服务 Base URL
    - ``ROLESWAP_WORKFLOW_ID``   工作流 ID
    - ``ROLESWAP_API_KEY``       可选鉴权 Token
    - ``ROLESWAP_RESULT_TIMEOUT``单段结果轮询超时（秒）
    - ``ROLESWAP_POLL_INTERVAL`` 轮询间隔（秒）
    - ``ROLESWAP_HTTP_TIMEOUT``  HTTP 请求超时（秒）
    """

    base_url: str
    workflow_id: str
    api_key: str | None = None
    result_timeout: int = 600
    poll_interval: float = 5.0
    http_timeout: float = 120.0
    # 提交任务超时（base64 大 JSON 需更长时间），默认 600 秒
    submit_timeout: float = 600.0
    # 本地文件输入方式：base64（推荐，API 原生支持）| upload | auto
    input_mode: str = "base64"
    # base64 模式下单文件最大字节数（默认 80MB）
    max_base64_bytes: int = 80 * 1024 * 1024

    # 各端点路径（与文档保持一致，一般无需改动）
    submit_path: str = "/api/workflow/generate"
    result_path: str = "/api/workflow/result"
    upload_path: str = "/api/comfy/upload/file"
    view_path: str = "/api/comfy/view"

    @classmethod
    def from_env(cls) -> "RoleSwapConfig":
        """从环境变量构建配置。缺少必填项时抛出明确错误。"""
        base_url = os.getenv("ROLESWAP_BASE_URL", "").strip()
        workflow_id = os.getenv("ROLESWAP_WORKFLOW_ID", "").strip()

        if not base_url:
            raise ValueError(
                "缺少 ROLESWAP_BASE_URL。请在 .env 中配置推理服务地址，"
                "或在创建 RoleSwapConfig 时显式传入 base_url。"
            )
        if not workflow_id:
            raise ValueError(
                "缺少 ROLESWAP_WORKFLOW_ID。请在 .env 中配置工作流 ID，"
                "或在创建 RoleSwapConfig 时显式传入 workflow_id。"
            )

        api_key = os.getenv("ROLESWAP_API_KEY", "").strip() or None
        input_mode = os.getenv("ROLESWAP_INPUT_MODE", "base64").strip().lower()
        if input_mode not in {"base64", "upload", "auto"}:
            input_mode = "base64"
        max_base64_bytes = _get_int("ROLESWAP_MAX_BASE64_BYTES", 80 * 1024 * 1024)

        return cls(
            base_url=base_url.rstrip("/"),
            workflow_id=workflow_id,
            api_key=api_key,
            result_timeout=_get_int("ROLESWAP_RESULT_TIMEOUT", 600),
            poll_interval=_get_float("ROLESWAP_POLL_INTERVAL", 5.0),
            http_timeout=_get_float("ROLESWAP_HTTP_TIMEOUT", 120.0),
            submit_timeout=_get_float("ROLESWAP_SUBMIT_TIMEOUT", 600.0),
            input_mode=input_mode,
            max_base64_bytes=max_base64_bytes,
            upload_path=os.getenv(
                "ROLESWAP_UPLOAD_PATH", "/api/comfy/upload/file"
            ).strip(),
            view_path=os.getenv(
                "ROLESWAP_VIEW_PATH", "/api/comfy/view"
            ).strip(),
        )

    def url(self, path: str) -> str:
        """拼接完整 URL。"""
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
