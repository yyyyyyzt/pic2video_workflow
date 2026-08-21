#!/usr/bin/env python3
"""avatar_lab CLI 的内部实现。

拆出来的目的是让 avatar_lab.py 回到「只做参数解析和调度」的角色：
表格渲染、HTML 对照页、成对打分排名这些都不该和编排逻辑挤在一个文件里。

  table.py     终端表格（中文双宽对齐）
  report.py    结果汇总、HTML 对照页
  rating.py    成对比较的 Bradley-Terry 排名
"""

from __future__ import annotations

from .table import render_table, text_width

__all__ = ["render_table", "text_width"]
