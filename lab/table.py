#!/usr/bin/env python3
"""终端表格。中文按 2 列宽计算，否则表头和数据会错位。"""

from __future__ import annotations


def text_width(text: str) -> int:
    """按终端显示宽度算长度，CJK 字符占 2 列。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = []
    for i in range(cols):
        cell_widths = [text_width(r[i]) for r in rows] if rows else []
        widths.append(max([text_width(headers[i])] + cell_widths))

    def line(cells) -> str:
        return "  ".join(str(c) + " " * (widths[i] - text_width(c))
                         for i, c in enumerate(cells))

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)
