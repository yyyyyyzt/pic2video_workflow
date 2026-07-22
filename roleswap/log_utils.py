"""统一日志：设置 ROLESWAP_DEBUG=1 输出详细诊断信息。"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Optional

_CONFIGURED = False


def setup_logging(
    *,
    level: Optional[int] = None,
    sink: Optional[Callable[[str], None]] = None,
) -> logging.Logger:
    """初始化 roleswap 日志。``sink`` 可用于写入 worker.log。"""
    global _CONFIGURED
    logger = logging.getLogger("roleswap")
    if _CONFIGURED and sink is None:
        return logger

    if level is None:
        debug = os.getenv("ROLESWAP_DEBUG", "").strip().lower() in {"1", "true", "yes"}
        level = logging.DEBUG if debug else logging.INFO

    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    logger.addHandler(sh)

    if sink is not None:

        class _CallbackHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    sink(self.format(record))
                except Exception:  # noqa: BLE001
                    pass

        ch = _CallbackHandler()
        ch.setFormatter(fmt)
        ch.setLevel(level)
        logger.addHandler(ch)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str = "roleswap") -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def describe_input_ref(ref: str) -> str:
    """描述输入引用（避免日志打印完整 base64）。"""
    if ref.startswith("data:"):
        head = ref.split(",", 1)[0]
        return f"{head},<base64 len={len(ref)} chars>"
    if len(ref) > 160:
        return ref[:80] + "…" + ref[-40:]
    return ref
