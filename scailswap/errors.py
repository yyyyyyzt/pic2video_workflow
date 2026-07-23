"""统一异常体系。"""

from __future__ import annotations


class ScailSwapError(RuntimeError):
    """库内所有可预期错误的基类。"""


class EngineError(ScailSwapError):
    """生成引擎调用失败（网络、工作流错误、输出缺失等）。"""


class EngineOOMError(EngineError):
    """GPU 显存溢出。processor 捕获后会清显存并自动重试。"""


class InvalidInputError(ScailSwapError):
    """用户输入不合法（文件缺失、格式错误、参数越界等）。"""
